"""使用官方 MCP SDK 管理 stdio 生命周期；命令由部署者配置，不接受模型输入。"""

import asyncio
import os
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentflow.application.tools.runtime import Tool, ToolRuntime


@asynccontextmanager
async def connect_tools(
    command: str,
    args: list[str],
    runtime: ToolRuntime,
    *,
    allowed: set[str],
    requires_approval: bool = True,
):
    # 不把整个父进程环境（数据库密码/模型密钥）继承给外部 MCP 进程。
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if key in os.environ
    }
    params = StdioServerParameters(command=command, args=args, env=environment)
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        async with asyncio.timeout(15):
            await session.initialize()
            result = await session.list_tools()
        selected = {tool.name: tool for tool in result.tools if tool.name in allowed}
        if selected.keys() != allowed:
            raise ValueError("MCP allowlist contains unavailable tools")
        for name, info in selected.items():

            async def invoke(arguments: dict, name=name):
                response = await session.call_tool(name, arguments)
                if response.isError:
                    raise RuntimeError("MCP tool failed")
                return response.model_dump(mode="json")

            runtime.register(
                Tool(
                    name,
                    info.description or name,
                    info.inputSchema,
                    invoke,
                    requires_approval,
                )
            )
        # 工具仅能在此上下文内调用；退出时 SDK 关闭管道和子进程。
        yield runtime
