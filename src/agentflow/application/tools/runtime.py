"""工具调用的单一入口：白名单、JSON Schema、审批和超时都在这里执行。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from agentflow.domain.agent.action import ErrorCode
from agentflow.domain.agent.status import ToolSideEffect


class ToolDeniedError(Exception):
    pass


class ToolConfirmedFailure(Exception):
    """工具适配器确认执行失败；不得携带原始异常正文。"""

    def __init__(self, code: str = "ToolFailed"):
        self.code = TypeAdapter(ErrorCode).validate_python(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ToolInvocation:
    action_id: str
    idempotency_key: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    invoke: Callable[[dict], Awaitable[Any]]
    requires_approval: bool = False
    timeout: float = 30
    side_effect: ToolSideEffect = ToolSideEffect.UNSAFE
    invoke_durable: Callable[[dict, ToolInvocation], Awaitable[Any]] | None = None


class ToolRuntime:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def side_effect(self, name: str) -> ToolSideEffect:
        tool = self._tools.get(name)
        return tool.side_effect if tool else ToolSideEffect.UNSAFE

    def prepare(
        self,
        name: str,
        arguments: dict,
        *,
        allowed: set[str],
        side_effect: ToolSideEffect,
    ) -> Tool:
        if name not in allowed or name not in self._tools:
            raise ToolDeniedError("tool is not allowed")
        tool = self._tools[name]
        if tool.requires_approval or tool.side_effect == ToolSideEffect.UNSAFE:
            raise ToolDeniedError("action approval is required")
        if tool.side_effect != side_effect:
            raise ToolDeniedError("tool policy changed since proposal")
        Draft202012Validator(tool.schema).validate(arguments)
        return tool

    async def call_durable(
        self,
        name: str,
        arguments: dict,
        invocation: ToolInvocation,
        *,
        allowed: set[str],
        side_effect: ToolSideEffect,
    ) -> Any:
        tool = self.prepare(name, arguments, allowed=allowed, side_effect=side_effect)
        async with asyncio.timeout(tool.timeout):
            if tool.invoke_durable is not None:
                return await tool.invoke_durable(arguments, invocation)
            return await tool.invoke(arguments)

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
