import pytest
from sqlalchemy import func, select

from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import AgentSessionRow
from agentflow.infrastructure.persistence.store import Store
from agentflow.interfaces.cli.main import serve_worker


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_bootstrap_explicit_mode_preserves_legacy(tmp_path, durable):
    store = Store(f"sqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    store.create_schema()
    try:
        run = store.submit(
            store.register(
                WorkflowSpec(
                    name="test",
                    steps=(
                        StepSpec(
                            id="work",
                            handler="agent",
                            parameters={"agent": {"mode": "durable_v1"}}
                            if durable
                            else {},
                        ),
                    ),
                )
            ),
            {},
        )
        await Worker(store, build_handlers(Settings(), store=store)).tick()
        assert store.get_run(run)["status"] == "succeeded"
        assert store.get_run(run)["steps"][0]["output"]["rounds"] == 1
        with store.transaction() as db:
            assert db.scalar(select(func.count()).select_from(AgentSessionRow)) == int(
                durable
            )
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["legacy", "store", "kwargs"])
async def test_cli_factory_injection_is_backward_compatible(tmp_path, style):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    store = Store(url)
    store.create_schema()
    store.close()
    seen = []

    def legacy(settings, tools):
        seen.append("legacy")
        return HandlerRegistry()

    def modern(settings, tools, *, store):
        seen.append(isinstance(store, Store))
        return HandlerRegistry()

    def keywords(settings, tools, **kwargs):
        return modern(settings, tools, **kwargs)

    await serve_worker(
        Settings(database_url=url),
        once=True,
        handler_factory={"legacy": legacy, "store": modern, "kwargs": keywords}[style],
    )
    assert seen == (["legacy"] if style == "legacy" else [True])
