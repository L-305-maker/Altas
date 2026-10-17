from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, text
from test_agent_store import final_response
from test_durable_agent_runner import store as store
from test_provider_admission import opened, policy, reservation

from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


def core_facts(store):
    with store.engine.connect() as db:
        metadata = MetaData()
        metadata.reflect(db)
        return {
            table.name: [
                dict(row)
                for row in db.execute(
                    select(
                        *[
                            c
                            for c in table.c
                            if not (
                                table.name == "agent_sessions"
                                and c.name == "provider_key"
                            )
                            and not (
                                table.name == "runs" and c.name == "usage_governance"
                            )
                        ]
                    ).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in metadata.sorted_tables
            if table.name
            not in {"alembic_version", "provider_admissions", "provider_permits"}
        }


def test_0004_upgrade_and_0005_downgrade_preserve_ledger_and_workflow(
    store, monkeypatch
):
    agents = SQLAgentStore(store)
    task, state = opened(agents)
    state = agents.commit_transition(task, reservation(state))
    state = agents.complete_agent_step(task, final_response(state), {"text": "old"})
    original = core_facts(store)
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    try:
        command.stamp(config, "head")
        command.downgrade(config, "0004")
        assert core_facts(store) == original
        command.upgrade(config, "head")
        command.check(config)
        assert core_facts(store) == original
        enabled = SQLAgentStore(store, admission_policy=policy())
        task, ready = opened(enabled)
        admitted = enabled.reserve_model_attempt(task, reservation(ready))
        enabled.complete_agent_step(
            task, final_response(admitted.state), {"text": "new"}
        )
        all_core = core_facts(store)
        store.engine.dispose()
        command.upgrade(config, "head")
        assert core_facts(store) == all_core
        with enabled._operator_transaction(ready.snapshot.id) as (_, _, _, _, restored):
            assert restored.model_permits[0].id == admitted.permit.id
            assert restored.model_permits[0].status == "released"
        command.downgrade(config, "0004")
        assert core_facts(store) == all_core
        command.upgrade(config, "head")
        command.check(config)
        assert core_facts(store) == all_core
    finally:
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
