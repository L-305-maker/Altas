import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, text
from test_action_review_runner import setup
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_model_governance import submit, worker

from agentflow.application.orchestration.worker import Worker
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


def old_facts(store):
    with store.engine.connect() as db:
        metadata = MetaData()
        metadata.reflect(db)
        return {
            t.name: [
                dict(row)
                for row in db.execute(
                    select(*[c for c in t.c if c.name != "request_contract"]).order_by(
                        *t.primary_key.columns
                    )
                ).mappings()
            ]
            for t in metadata.sorted_tables
            if t.name != "alembic_version"
        }


@pytest.mark.asyncio
async def test_0008_roundtrip_preserves_old_facts_and_never_backfills_identity(
    store, monkeypatch, runtime_invariants
):
    run = submit(store)
    assert await worker(store, MockProvider([final()])).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0].request_contract is not None
    reviewed, agents, _, _, invoked, handlers, _ = setup(store, unknown=True)
    executor = Worker(store, handlers)
    assert await executor.tick()
    pending = runtime_invariants(store, reviewed)["work"].agent
    approval = pending.approvals[0]
    agents.decide_action(
        approval.approval_id,
        ApprovalDecision(
            expected_revision=pending.snapshot.revision,
            decision="approved",
            actor="test",
        ),
    )
    assert await executor.tick()
    waiting = runtime_invariants(store, reviewed)["work"].agent
    agents.reconcile_action(
        waiting.snapshot.id,
        approval.action_id,
        ReconciliationDecision(
            request_id="migration",
            expected_revision=waiting.snapshot.revision,
            confirmed_status="succeeded",
            result={"value": 1},
            actor="test",
            evidence_reference="test",
        ),
    )
    assert await executor.tick()
    assert len(invoked) == 1 and len(store.artifacts(reviewed)) == 1
    facts = old_facts(store)
    assert (
        facts["agent_actions"]
        and facts["agent_action_approvals"]
        and facts["agent_action_reconciliations"]
        and facts["artifacts"]
    )
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    try:
        command.stamp(config, "head")
        command.downgrade(config, "0007")
        assert old_facts(store) == facts
        command.upgrade(config, "head")
        command.check(config)
        assert old_facts(store) == facts
        with SQLAgentStore(store)._operator_transaction(state.snapshot.id) as (
            _,
            _,
            _,
            _,
            restored,
        ):
            assert restored.model_attempts[0].request_contract is None
            assert (
                restored.model_attempts[0].context_projection
                == state.model_attempts[0].context_projection
            )
            assert restored.snapshot == state.snapshot
    finally:
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
