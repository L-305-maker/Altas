"""仅指向专用临时数据库！本文件会清空测试库中的 AgentFlow 表。"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, update

from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import Base, StepRow
from agentflow.infrastructure.persistence.store import Store

URL = os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="requires a dedicated PostgreSQL test database"
)


@pytest.fixture
def pgstore():
    store = Store(URL)
    Base.metadata.drop_all(store.engine)
    store.create_schema()
    yield store
    Base.metadata.drop_all(store.engine)
    store.close()


def test_postgres_concurrent_workflow_registration(pgstore):
    spec = WorkflowSpec(
        name="concurrent-registration",
        steps=(StepSpec(id="a", handler="echo"),),
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: pgstore.register(spec), range(8)))
    assert len(set(ids)) == 1


def test_postgres_workers_claim_without_duplicates(pgstore):
    spec = WorkflowSpec(
        name="parallel",
        steps=tuple(StepSpec(id=f"step{i}", handler="echo") for i in range(12)),
    )
    run = pgstore.submit(pgstore.register(spec), {})

    # SKIP LOCKED 允许暂时无任务，因此 worker 应继续轮询，而非假设一次调用一定领取成功。
    def consume(_):
        result = []
        for _ in range(30):
            task = pgstore.claim()
            if task:
                result.append(task.step_id)
                pgstore.complete(task, {"ok": True})
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [item for batch in pool.map(consume, range(4)) for item in batch]
    assert len(ids) == len(set(ids)) == 12
    assert pgstore.get_run(run)["status"] == "succeeded"


def test_postgres_recovery_rejects_old_token(pgstore):
    workflow = pgstore.register(
        WorkflowSpec(
            name="recover", steps=(StepSpec(id="a", handler="echo", retry_delay=0),)
        )
    )
    pgstore.submit(workflow, {})
    old = pgstore.claim()
    with pgstore.transaction() as session:
        session.execute(update(StepRow).values(lease_until=0))
    new = pgstore.claim()
    assert not pgstore.complete(old, "old")
    assert pgstore.complete(new, "new")


def test_postgres_migrations_roundtrip(monkeypatch):
    store = Store(URL)
    Base.metadata.drop_all(store.engine)
    monkeypatch.setenv("AGENTFLOW_DATABASE_URL", URL)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    assert "steps" in inspect(store.engine).get_table_names()
    command.check(config)
    command.downgrade(config, "base")
    assert "steps" not in inspect(store.engine).get_table_names()
    store.close()


def test_postgres_agent_lease_uses_database_clock(pgstore, monkeypatch):
    import agentflow.infrastructure.persistence.store as store_module
    from agentflow.domain.agent.budget import AgentBudget
    from agentflow.domain.agent.run import AgentSnapshot
    from agentflow.infrastructure.persistence.agent_store import SQLAgentStore

    run = pgstore.submit(
        pgstore.register(
            WorkflowSpec(
                name="agent-clock",
                steps=(StepSpec(id="work", handler="agent"),),
            )
        ),
        {},
    )
    task = pgstore.claim(300)
    agents = SQLAgentStore(pgstore)
    initial = AgentSnapshot(
        id="clock-agent",
        budget=AgentBudget(
            max_model_rounds=1,
            max_model_attempts=1,
            max_tool_calls=1,
            max_resource_units=100,
        ),
    )
    agents.open(task, initial)
    monkeypatch.setattr(store_module.time, "time", lambda: 10**15)
    assert agents.load(task).snapshot == initial
    assert pgstore.heartbeat(task, 300)
    assert pgstore.get_run(run)["status"] == "running"
