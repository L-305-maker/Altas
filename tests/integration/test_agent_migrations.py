"""0001 的真实 Workflow 数据经过升级、回退、再升级后仍可执行。"""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text

from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import Base, RunRow, StepRow
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_agent_migration_roundtrip_preserves_existing_workflows(
    backend, tmp_path, monkeypatch
):
    if backend == "postgres":
        url = os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("requires a dedicated PostgreSQL test database")
    else:
        url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setenv("AGENTFLOW_DATABASE_URL", url)
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    with store.engine.begin() as db:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0001")
        run_id = store.submit(
            store.register(
                WorkflowSpec(
                    name="before-agent-migration",
                    steps=(StepSpec(id="work", handler="echo"),),
                )
            ),
            {"value": 42},
        )
        with store.transaction() as db:
            step_id = db.scalar(select(StepRow.id).where(StepRow.run_id == run_id))
        command.upgrade(config, "0002")
        tables = inspect(store.engine).get_table_names()
        assert {
            "agent_sessions",
            "agent_messages",
            "agent_model_calls",
            "agent_actions",
        } <= set(tables)
        command.upgrade(config, "head")  # 当前实现只在当前 schema 上运行。
        task = store.claim(300)
        SQLAgentStore(store).open(
            task,
            AgentSnapshot(
                id="migration-agent",
                budget=AgentBudget(
                    max_model_rounds=1,
                    max_model_attempts=1,
                    max_tool_calls=1,
                    max_resource_units=100,
                ),
            ),
        )
        command.check(config)
        command.downgrade(config, "0001")
        assert "agent_sessions" not in inspect(store.engine).get_table_names()
        with store.transaction() as db:
            # Inspect only columns present in 0001; current ORM has newer fields.
            assert db.scalar(select(RunRow.inputs).where(RunRow.id == run_id)) == {
                "value": 42
            }
            assert db.get(StepRow, step_id).token == task.token
        command.upgrade(config, "head")
        assert store.complete(task, {"preserved": True})
        assert store.get_run(run_id)["status"] == "succeeded"
    finally:
        command.downgrade(config, "base")
        store.close()
