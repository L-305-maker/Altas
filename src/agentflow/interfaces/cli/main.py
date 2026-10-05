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


async def serve_worker(settings: Settings, *, once: bool = False) -> None:
    store = Store(settings.database_url.get_secret_value())
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
                build_handlers(settings, tools),
                lease_seconds=settings.lease_seconds,
                concurrency=settings.concurrency,
                poll_seconds=settings.poll_seconds,
            )
            if once:
                await worker.tick()
            else:
                await worker.serve(asyncio.Event())
    finally:
        store.close()


def main() -> None:
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
            "agentflow.interfaces.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
        )
    else:
        try:
            asyncio.run(serve_worker(settings, once=args.once))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
