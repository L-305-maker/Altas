import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_action_review_runner import setup

from agentflow.application.orchestration.worker import Worker
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.infrastructure.persistence.models import Base
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_normal_head_upgrade_preserves_valid_reviews_and_restores_execution(
    backend, tmp_path, monkeypatch, runtime_invariants
):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if backend == "postgres"
        else f"sqlite:///{(tmp_path / 'release.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires dedicated PostgreSQL database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    with store.engine.begin() as db:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
    monkeypatch.setenv("AGENTFLOW_DATABASE_URL", url)
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")
        command.check(config)
        run, agents, _, provider, invoked, handlers, _ = setup(store, unknown=True)
        worker = Worker(store, handlers)
        assert await worker.tick()
        approval = agents.list_pending_action_approvals()[0]
        state = runtime_invariants(store, run)["work"].agent
        agents.decide_action(
            approval.approval_id,
            ApprovalDecision(
                expected_revision=state.snapshot.revision,
                decision="approved",
                actor="operator",
            ),
        )
        assert await worker.tick()
        unknown = runtime_invariants(store, run)["work"].agent
        agents.reconcile_action(
            approval.session_id,
            approval.action_id,
            ReconciliationDecision(
                request_id="release-evidence",
                expected_revision=unknown.snapshot.revision,
                confirmed_status="succeeded",
                result={"value": 1},
                evidence_reference="external-record",
                actor="operator",
            ),
        )
        before = runtime_invariants(store, run)
        store.engine.dispose()
        command.upgrade(config, "head")  # 正常升级不能像 downgrade 那样丢弃审计。
        command.check(config)
        assert runtime_invariants(store, run) == before
        task = store.claim(300)
        restored = agents.load(task)
        assert restored == before["work"].agent
        assert restored.approvals[0].consumed_at is not None
        assert len(restored.reconciliations) == 1
        # 已领取 Task 经同一 Runner 完成；不重复外部工具。
        from agentflow.application.agents.durable_runner import DurableAgentRunner
        from agentflow.application.tools.runtime import ToolRuntime

        await DurableAgentRunner(agents, provider, ToolRuntime()).run(
            task, "goal", allowed=set()
        )
        runtime_invariants(store, run)
        assert store.get_run(run)["status"] == "succeeded" and len(invoked) == 1
    finally:
        command.downgrade(config, "base")
        store.close()
