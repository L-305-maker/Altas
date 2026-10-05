"""An application-owned Agent workflow; no changes to AgentFlow are needed."""

import asyncio
import tempfile
from pathlib import Path

from agentflow.application.orchestration.worker import Worker
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.store import Store


async def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(f"sqlite:///{Path(directory).as_posix()}/agent.db")
        try:
            store.create_schema()
            handlers = build_handlers(Settings(provider="mock"))
            spec = WorkflowSpec(
                name="simple_agent",
                steps=(
                    StepSpec(
                        id="answer", handler="agent", parameters={"prompt": "Hello"}
                    ),
                ),
            )
            run_id = store.submit(store.register(spec), {})
            await Worker(store, handlers).tick()
            assert store.get_run(run_id)["status"] == "succeeded"
            print(store.get_run(run_id)["steps"][0]["output"])
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
