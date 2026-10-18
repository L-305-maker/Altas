"""0007 adds identity only; prior attempts stay explicitly unprojected/unmeasured."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, inspect, select, text
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_model_governance import submit, worker

from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


def historical_facts(store):
    with store.engine.connect() as db:
        metadata = MetaData()
        metadata.reflect(db)
        return {
            t.name: [
                dict(row)
                for row in db.execute(
                    select(
                        *[c for c in t.c if c.name != "context_projection"]
                    ).order_by(*t.primary_key.columns)
                ).mappings()
            ]
            for t in metadata.sorted_tables
            if t.name != "alembic_version"
        }


@pytest.mark.asyncio
async def test_0007_upgrade_downgrade_preserves_old_data_without_inventing_manifests(
    store, monkeypatch, runtime_invariants
):
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    assert await worker(store, MockProvider([final()])).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0].context_projection is not None
    before = historical_facts(store)
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    try:
        command.stamp(config, "head")
        command.downgrade(config, "0006")
        assert "context_projection" not in {
            c["name"] for c in inspect(store.engine).get_columns("agent_model_attempts")
        }
        assert historical_facts(store) == before
        command.upgrade(config, "head")
        command.check(config)
        assert historical_facts(store) == before
        agents = SQLAgentStore(store)
        with agents._operator_transaction(state.snapshot.id) as (_, _, _, _, restored):
            assert restored.model_attempts[0].context_projection is None
            assert restored.model_dump(exclude={"model_attempts"}) == state.model_dump(
                exclude={"model_attempts"}
            )
        command.upgrade(config, "head")
        assert historical_facts(store) == before
    finally:
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
