from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, text
from test_agent_store import final_response
from test_durable_agent_runner import store as store
from test_provider_admission import opened, policy, reservation

from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


def facts(store):
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
                                table.name == "runs" and c.name == "usage_governance"
                            )
                            and not (
                                table.name == "agent_model_attempts"
                                and c.name in {"context_projection", "request_contract"}
                            )
                        ]
                    ).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in metadata.sorted_tables
            if table.name != "alembic_version"
        }


def test_0006_roundtrip_preserves_all_attempts_actions_and_artifacts(
    store, monkeypatch
):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    state = agents.reserve_model_attempt(task, reservation(ready)).state
    completed = agents.complete_agent_step(
        task, final_response(state), {"text": "saved"}
    )
    before = facts(store)
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    try:
        command.stamp(config, "head")
        command.downgrade(config, "0005")
        assert facts(store) == before
        command.upgrade(config, "head")
        command.check(config)
        assert facts(store) == before
        with agents._operator_transaction(completed.snapshot.id) as (
            _,
            _,
            _,
            _,
            restored,
        ):
            assert restored == completed
    finally:
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
