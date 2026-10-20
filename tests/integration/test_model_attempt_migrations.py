import json
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from test_agent_store import agents as agents
from test_agent_store import (
    candidate,
    dispatch,
    expire,
    final_response,
    finish_action,
    reserve,
    response,
    start,
)

from agentflow.application.ports.agent_store import AgentTransition, ModelCallResult
from agentflow.application.ports.model_request import ModelRequestError
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import Base
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_attempt_upgrade_backfills_unmeasured_and_preserves_all_old_facts(
    backend, tmp_path, monkeypatch
):
    from test_durable_model_request import reservation_with_contract

    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if backend == "postgres"
        else f"sqlite:///{(tmp_path / 'usage-migration.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires dedicated PostgreSQL database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    with store.engine.begin() as db:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
    monkeypatch.setenv("AGENTFLOW_DATABASE_URL", url)
    config = Config("alembic.ini")

    def facts():
        with store.engine.connect() as db:
            return {
                t.name: [
                    dict(r)
                    for r in db.execute(
                        select(
                            *[
                                c
                                for c in t.c
                                if not (
                                    t.name == "agent_sessions"
                                    and c.name == "provider_key"
                                )
                                and not (
                                    t.name == "runs" and c.name == "usage_governance"
                                )
                            ]
                        )
                    ).mappings()
                ]
                for t in Base.metadata.sorted_tables
                if t.name
                not in {
                    "agent_model_attempts",
                    "provider_admissions",
                    "provider_permits",
                }
            }

    try:
        command.upgrade(config, "head")
        agents = SQLAgentStore(store)
        task, state = start(agents, artifact=True)
        state = agents.commit_transition(task, reservation_with_contract(state))
        state = agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(state.snapshot, status="ready_model"),
                model_result=ModelCallResult(
                    model_call_id=state.snapshot.model_call_id, status="failed"
                ),
            ),
        )
        state = agents.commit_transition(task, reservation_with_contract(state))
        proposal = response(state).model_dump()
        proposal["messages"][0]["payload"]["message"] = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "vendor-1",
                    "function": {
                        "name": "lookup",
                        "arguments": json.dumps(proposal["arguments"][0]["arguments"]),
                    },
                }
            ],
        }
        state = agents.commit_transition(task, AgentTransition.model_validate(proposal))
        state = finish_action(agents, task, dispatch(agents, task, state))
        state = agents.commit_transition(
            task,
            AgentTransition(snapshot=candidate(state.snapshot, status="ready_model")),
        )
        state = agents.commit_transition(task, reservation_with_contract(state))
        state = agents.complete_agent_step(task, final_response(state), {"value": 42})
        original = facts()
        command.downgrade(config, "0003")
        assert "agent_model_attempts" not in inspect(store.engine).get_table_names()
        assert facts() == original
        command.upgrade(config, "head")
        command.check(config)
        assert facts() == original
        with agents._operator_transaction(state.snapshot.id) as (_, _, _, _, restored):
            assert restored.snapshot == state.snapshot
            assert restored.actions == state.actions
            assert restored.messages == state.messages
            assert restored.model_calls == state.model_calls
            assert restored.snapshot.budget.model_attempts == 3
            assert [a.attempt_index for a in restored.model_attempts] == [1, 2, 1]
            assert [a.outcome for a in restored.model_attempts] == [
                "LEGACY_UNMEASURED",
                "succeeded",
                "succeeded",
            ]
            assert all(
                a.accounting == "LEGACY_UNMEASURED" for a in restored.model_attempts
            )
            assert all(
                a.created_at is None and a.confirmed_at is None
                for a in restored.model_attempts
            )
            assert all(a.raw_usage is None for a in restored.model_attempts)
        summary = agents.usage_summary(state.snapshot.id)
        assert summary.attempts == summary.legacy_unmeasured_attempts == 3
        assert summary.total_tokens.value is None
        command.downgrade(config, "0003")
        assert facts() == original
        command.upgrade(config, "head")
        assert facts() == original
        assert agents.usage_summary(state.snapshot.id) == summary
    finally:
        command.downgrade(config, "base")
        store.close()


@pytest.mark.parametrize("confirmed_failure", [False, True])
def test_legacy_pending_attempts_recover_without_reset_or_invented_usage(
    agents, monkeypatch, confirmed_failure
):
    task, state = start(agents)
    state = reserve(agents, task, state)
    if confirmed_failure:
        state = agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(state.snapshot, status="ready_model"),
                model_result=ModelCallResult(model_call_id="model-1", status="failed"),
            ),
        )
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL",
        agents.store.engine.url.render_as_string(hide_password=False),
    )
    try:
        command.stamp(config, "head")  # Fixture creates the current schema directly.
        command.downgrade(config, "0003")
        command.upgrade(config, "head")
        restored = agents.load(task)
        assert restored.snapshot == state.snapshot
        assert restored.model_attempts[0].accounting == "LEGACY_UNMEASURED"
        if confirmed_failure:
            with pytest.raises(
                ModelRequestError, match="ModelRequestContractUnavailable"
            ):
                reserve(agents, task, restored)
            assert agents.load(task) == restored
            assert restored.snapshot.budget.model_attempts == 1
            assert len(restored.model_attempts) == 1
        else:
            expire(agents, task)
            replacement = agents.store.claim(300)
            restored = agents.load(replacement)
            assert restored.model_attempts[0].outcome == "unknown"
            restored = agents.fail_agent_step(
                replacement,
                AgentTransition(
                    snapshot=candidate(restored.snapshot, status="failed"),
                    model_result=ModelCallResult(
                        model_call_id="model-1", status="unknown"
                    ),
                ),
            )
            assert restored.snapshot.budget.model_attempts == 1
        assert restored.usage_summary().legacy_unmeasured_attempts == 1
        assert restored.usage_summary().total_tokens.value is None
    finally:
        with agents.store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
