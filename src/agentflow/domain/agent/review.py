"""行动绑定与操作员审计契约；证据是操作者声明，不是 Core 对外部事实的证明。"""

import json
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    model_validator,
)

from agentflow.domain.agent.action import Action, ErrorCode, Identifier
from agentflow.domain.agent.budget import Counter
from agentflow.domain.agent.status import ActionStatus, ToolSideEffect
from agentflow.domain.workflow.spec import Name

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AuditLabel = Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]


class ToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_name: Name
    tool_version: Identifier
    tool_policy_digest: Digest
    side_effect: ToolSideEffect
    requires_approval: bool

    @property
    def protected(self) -> bool:
        return self.requires_approval or self.side_effect == ToolSideEffect.UNSAFE

    def matches(self, action: Action) -> bool:
        return (
            self.tool_name == action.tool_name
            and self.side_effect == action.side_effect
        )


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: Counter
    decision: Literal["approved", "rejected"]
    actor: AuditLabel = Field(
        description="Audit label supplied by the authenticated single-tenant operator; not independently verified identity"
    )
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_text(self) -> Self:
        if any("\0" in v for v in (self.actor, self.reason)):
            raise ValueError("operator text cannot contain NUL")
        return self


class ActionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: Identifier
    session_id: Identifier
    action_id: Identifier
    binding: ToolBinding
    arguments_digest: Digest
    decision: Literal["pending", "approved", "rejected", "cancelled"]
    actor: AuditLabel | None = None
    reason: str | None = Field(default=None, max_length=1000)
    created_at: FiniteFloat
    decided_at: FiniteFloat | None = None
    consumed_at: FiniteFloat | None = None

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if self.decision == "pending":
            if any(
                v is not None
                for v in (self.actor, self.reason, self.decided_at, self.consumed_at)
            ):
                raise ValueError("pending approval cannot contain decision facts")
        elif self.decided_at is None:
            raise ValueError("decided approval requires a decision timestamp")
        if self.decision in {"approved", "rejected"} and self.actor is None:
            raise ValueError("operator decision requires an audit label")
        if self.consumed_at is not None and self.decision != "approved":
            raise ValueError("only approved authorization can be consumed")
        return self

    def authorizes(self, action: Action, binding: ToolBinding) -> bool:
        return (
            self.decision == "approved"
            and self.session_id == action.agent_id
            and self.action_id == action.id
            and self.binding == binding
            and binding.matches(action)
            and self.arguments_digest == action.arguments_digest
        )


class ReconciliationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: Identifier
    expected_revision: Counter
    confirmed_status: Literal["succeeded", "failed"]
    result: JsonValue = None
    error_code: ErrorCode | None = None
    evidence_reference: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    actor: AuditLabel = Field(
        description="Audit label supplied by the authenticated single-tenant operator; not independently verified identity"
    )
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def safe_outcome(self) -> Self:
        if any(
            "\0" in v
            for v in (self.request_id, self.actor, self.reason, self.evidence_reference)
        ):
            raise ValueError("operator text cannot contain NUL")
        if (self.confirmed_status == "failed") != (self.error_code is not None):
            raise ValueError("failed reconciliation requires a safe error code")
        if len(json.dumps(self.result, allow_nan=False).encode()) > 100_000:
            raise ValueError("reconciliation result exceeds storage limit")
        return self


class ActionReconciliation(ReconciliationDecision):
    session_id: Identifier
    action_id: Identifier
    previous_status: Literal[ActionStatus.UNKNOWN]
    applied: bool
    created_at: FiniteFloat


class ActionExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Action
    binding: ToolBinding | None
    approval: ActionApproval | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)
