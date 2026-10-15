"""Action 只保留调用元数据；不保存参数正文、异常消息或工具 SDK 对象。"""

import hashlib
import json
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentflow.domain.agent.status import ActionStatus, ToolSideEffect
from agentflow.domain.execution.exception import InvalidStateTransitionError
from agentflow.domain.workflow.spec import Name

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
ErrorCode = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")]

_ACTION_TRANSITIONS = MappingProxyType(
    {
        ActionStatus.PROPOSED: frozenset({ActionStatus.INFLIGHT, ActionStatus.DENIED}),
        ActionStatus.INFLIGHT: frozenset(
            {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN}
        ),
    }
)


def digest_arguments(arguments: dict[str, JsonValue]) -> str:
    """对规范 JSON 求 SHA-256；摘要用于一致性检查，不提供加密保密性。"""
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Action(BaseModel):
    """ID 由调用方分配；同一 Action 只执行一次，失败也是终态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    agent_id: Identifier
    model_call_id: Identifier
    tool_name: Name
    arguments_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    side_effect: ToolSideEffect = ToolSideEffect.UNSAFE
    status: ActionStatus = ActionStatus.PROPOSED
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_error(self) -> Self:
        needs_error = self.status in {
            ActionStatus.FAILED,
            ActionStatus.UNKNOWN,
            ActionStatus.DENIED,
        }
        if needs_error != (self.error_code is not None):
            raise ValueError(
                "error_code is required only for failed, unknown or denied"
            )
        return self

    def transition(
        self, target: ActionStatus, *, error_code: str | None = None
    ) -> Self:
        target = ActionStatus(target)
        if target not in _ACTION_TRANSITIONS.get(self.status, frozenset()):
            raise InvalidStateTransitionError(f"action: {self.status} -> {target}")
        return self._with_outcome(target, error_code)

    def reconcile(self, target: ActionStatus, *, error_code: str | None = None) -> Self:
        """调用方已取得外部证据后确认结果；从不重新执行 UNKNOWN。"""
        target = ActionStatus(target)
        if self.status != ActionStatus.UNKNOWN or target not in {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
        }:
            raise InvalidStateTransitionError(
                f"reconciliation: {self.status} -> {target}"
            )
        return self._with_outcome(target, error_code)

    def _with_outcome(self, target: ActionStatus, error_code: str | None) -> Self:
        return type(self).model_validate(
            {**self.model_dump(), "status": target, "error_code": error_code}
        )
