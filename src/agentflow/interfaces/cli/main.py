"""命令行入口；命令复用相同组合根，不复制业务逻辑。"""

import argparse
import asyncio
from contextlib import AsyncExitStack

import uvicorn

from agentflow.application.orchestration.worker import Worker
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.infrastructure.mcp.client import connect_tools
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.telemetry.tracing import local_tracing


async def serve_worker(
    settings: Settings, *, once: bool = False, handler_factory=build_handlers
) -> None:
    store = Store(settings.database_url.get_secret_value())
    tracing = local_tracing() if settings.telemetry else None
    try:
        async with AsyncExitStack() as stack:
            tools = ToolRuntime()
            if settings.mcp_command:
                await stack.enter_async_context(
                    connect_tools(
                        settings.mcp_command,
                        settings.mcp_args,
                        tools,
                        allowed=set(settings.mcp_tools),
                    )
                )
            worker = Worker(
                store,
                handler_factory(settings, tools),
                lease_seconds=settings.lease_seconds,
                concurrency=settings.concurrency,
                poll_seconds=settings.poll_seconds,
                tracer=tracing.get_tracer("agentflow.worker") if tracing else None,
            )
            if once:
                await worker.tick()
            else:
                await worker.serve(asyncio.Event())
    finally:
        store.close()
        if tracing:
            tracing.shutdown()


def main(
    *,
    api_factory="agentflow.interfaces.api.app:create_app",
    handler_factory=build_handlers,
) -> None:
    parser = argparse.ArgumentParser(description="AgentFlow 持久工作流引擎")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="仅用于本地演示；生产使用 alembic upgrade head")
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        store = Store(settings.database_url.get_secret_value())
        try:
            store.create_schema()
        finally:
            store.close()
    elif args.command == "api":
        uvicorn.run(
            api_factory,
            factory=True,
            host=args.host,
            port=args.port,
            ws_max_size=16384,
        )
    else:
        try:
            asyncio.run(
                serve_worker(settings, once=args.once, handler_factory=handler_factory)
            )
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
