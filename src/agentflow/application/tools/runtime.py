"""工具调用的单一入口：白名单、JSON Schema、审批和超时都在这里执行。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator


class ToolDeniedError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    invoke: Callable[[dict], Awaitable[Any]]
    requires_approval: bool = False
    timeout: float = 30


class ToolRuntime:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        Draft202012Validator.check_schema(tool.schema)
        self._tools[tool.name] = tool

    def schemas(self, allowed: set[str]) -> list[dict]:
        if allowed - self._tools.keys():
            raise ToolDeniedError("unknown tool in allowlist")
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": self._tools[name].description,
                    "parameters": self._tools[name].schema,
                },
            }
            for name in sorted(allowed)
        ]

    async def call(
        self, name: str, arguments: dict, *, allowed: set[str], approved: bool = False
    ) -> Any:
        if name not in allowed or name not in self._tools:
            raise ToolDeniedError("tool is not allowed")
        tool = self._tools[name]
        if tool.requires_approval and not approved:
            raise ToolDeniedError("tool requires an approved workflow step")
        Draft202012Validator(tool.schema).validate(arguments)
        async with asyncio.timeout(tool.timeout):
            return await tool.invoke(arguments)
