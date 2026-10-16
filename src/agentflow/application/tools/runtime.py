"""工具调用的单一入口：白名单、JSON Schema、审批和超时都在这里执行。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from agentflow.domain.agent.action import ErrorCode, Identifier, digest_arguments
from agentflow.domain.agent.review import ActionExecutionContext, ToolBinding
from agentflow.domain.agent.status import ActionStatus, ToolSideEffect


class ToolDeniedError(Exception):
    pass


class ToolApprovalRequired(ToolDeniedError):
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
    version: str = "1"


class ToolRuntime:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def side_effect(self, name: str) -> ToolSideEffect:
        tool = self._tools.get(name)
        return tool.side_effect if tool else ToolSideEffect.UNSAFE

    def binding(self, name: str) -> ToolBinding | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return ToolBinding(
            tool_name=tool.name,
            tool_version=tool.version,
            tool_policy_digest=digest_arguments(
                {
                    "name": tool.name,
                    "version": tool.version,
                    "schema": tool.schema,
                    "side_effect": tool.side_effect.value,
                    "requires_approval": tool.requires_approval,
                    "timeout": tool.timeout,
                    "description": tool.description,
                }
            ),
            side_effect=tool.side_effect,
            requires_approval=tool.requires_approval,
        )

    def prepare(
        self,
        name: str,
        arguments: dict,
        *,
        allowed: set[str],
        side_effect: ToolSideEffect,
        context: ActionExecutionContext | None = None,
    ) -> Tool:
        if name not in allowed or name not in self._tools:
            raise ToolDeniedError("tool is not allowed")
        tool = self._tools[name]
        if tool.side_effect != side_effect:
            raise ToolDeniedError("tool policy changed since proposal")
        binding = self.binding(name)
        if context is not None and (
            context.binding is None
            or context.binding != binding
            or not binding.matches(context.action)
            or digest_arguments(arguments) != context.action.arguments_digest
        ):
            raise ToolDeniedError("action tool binding is missing or changed")
        Draft202012Validator(tool.schema).validate(arguments)
        if binding.protected:
            if context is None or context.approval is None:
                raise ToolApprovalRequired("action approval is required")
            if not context.approval.authorizes(context.action, binding):
                raise ToolDeniedError("action approval does not match this invocation")
        return tool

    async def call_durable(
        self,
        name: str,
        arguments: dict,
        invocation: ToolInvocation,
        *,
        allowed: set[str],
        side_effect: ToolSideEffect,
        context: ActionExecutionContext | None = None,
    ) -> Any:
        tool = self.prepare(
            name, arguments, allowed=allowed, side_effect=side_effect, context=context
        )
        if (
            context is None
            or context.action.status != ActionStatus.INFLIGHT
            or context.action.id != invocation.action_id
            or context.idempotency_key != invocation.idempotency_key
        ):
            raise ToolDeniedError("durable invocation requires a bound action context")
        if context.binding.protected and (
            context.approval is None or context.approval.consumed_at is None
        ):
            raise ToolDeniedError("action approval must be consumed before execution")
        async with asyncio.timeout(tool.timeout):
            if tool.invoke_durable is not None:
                return await tool.invoke_durable(arguments, invocation)
            return await tool.invoke(arguments)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        Draft202012Validator.check_schema(tool.schema)
        TypeAdapter(Identifier).validate_python(tool.version)
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
