"""Durable Agent 的事务契约；不依赖 SQLAlchemy 或模型供应商。

消息序号从 0 开始连续追加。assistant payload 的 action_ids 对应有序批次；
工具观察由 Store 根据 observations 生成，调用方不得伪造 tool 消息。
所有写入先持久化预约，再由调用方在事务外执行外部操作。
"""

from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentflow.application.ports.model_usage import (
    ModelUsage,
    ModelUsageSummary,
    summarize_usage,
)
from agentflow.application.ports.observed_usage import ObservedUsageReport
from agentflow.application.ports.provider_admission import (
    ModelPermit,
    ProviderBackpressure,
    ProviderKey,
)
from agentflow.domain.agent.action import Action, ErrorCode, Identifier
from agentflow.domain.agent.budget import Counter
from agentflow.domain.agent.review import (
    ActionApproval,
    ActionReconciliation,
    ApprovalDecision,
    ReconciliationDecision,
    ToolBinding,
)
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.execution.task import Task


class StateConflict(Exception):
    """状态、修订号或追加记录与已持久化执行不一致。"""


class LostLease(Exception):
    """Task 不再拥有未过期的执行租约。"""


class AgentDataError(ValueError):
    """数据不满足持久化契约；消息只含安全错误说明，不携带输入正文。"""


class AgentStoreUnavailable(Exception):
    """数据库操作失败，提交结果可能未知；恢复租约后重读，不能直接派发。"""


class ObservedUsageBlocked(Exception):
    """The owned current revision cannot reserve another measured attempt."""

    def __init__(self, report: ObservedUsageReport):
        self.report = report
        super().__init__(report.error_code)


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sequence: Counter
    role: Literal["system", "user", "assistant", "tool"]
    payload: dict[str, JsonValue]
    model_call_id: Identifier | None = None


class ModelCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_call_id: Identifier
    status: Literal["succeeded", "failed", "unknown"]
    usage: dict[str, JsonValue] | None = None
    normalized_usage: ModelUsage | None = None
    model: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    backpressure: ProviderBackpressure | None = None


class ModelAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: Identifier
    model_call_id: Identifier
    attempt_index: Annotated[int, Field(ge=1, strict=True)]
    outcome: Literal["inflight", "succeeded", "failed", "unknown", "LEGACY_UNMEASURED"]
    accounting: Literal["recorded", "LEGACY_UNMEASURED"] = "recorded"
    model: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    raw_usage: dict[str, JsonValue] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    created_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    confirmed_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent_measurements(self) -> Self:
        if self.accounting == "recorded":
            if self.outcome == "LEGACY_UNMEASURED" or self.created_at is None:
                raise ValueError("recorded attempts require reservation facts")
            if (self.outcome == "inflight") != (self.confirmed_at is None):
                raise ValueError("attempt outcome and confirmation must agree")
        if self.outcome in {"inflight", "unknown", "LEGACY_UNMEASURED"} or (
            self.accounting == "LEGACY_UNMEASURED"
        ):
            if self.raw_usage is not None or self.usage != ModelUsage():
                raise ValueError("unmeasured attempts cannot invent usage")
        if self.raw_usage is None and self.usage.source != "unavailable":
            raise ValueError("normalized usage requires raw provider measurements")
        if self.created_at is not None and self.confirmed_at is not None:
            if self.confirmed_at < self.created_at:
                raise ValueError("attempt cannot be confirmed before reservation")
        return self


class ModelCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_call_id: Identifier
    round_index: Annotated[int, Field(ge=1, strict=True)]
    status: Literal["inflight", "succeeded", "failed", "unknown"]
    attempts: Annotated[int, Field(ge=1, strict=True)]
    usage: dict[str, JsonValue] | None = None


class ActionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action_id: Identifier
    arguments: dict[str, JsonValue]
    tool_binding: ToolBinding | None = None


class ActionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sequence: Counter
    action_id: Identifier
    result: JsonValue


class StoredAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Action
    call_index: Counter
    arguments: dict[str, JsonValue]
    idempotency_key: str
    result: JsonValue = None
    tool_binding: ToolBinding | None = None


class AgentTransition(BaseModel):
    """一次原子提交：快照、消息、模型结果、参数及工具观察。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: AgentSnapshot
    messages: tuple[AgentMessage, ...] = ()
    model_result: ModelCallResult | None = None
    arguments: tuple[ActionArguments, ...] = ()
    observations: tuple[ActionObservation, ...] = ()
    error_code: ErrorCode | None = None
    authorizations: tuple[Identifier, ...] = ()


class AgentSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: AgentSnapshot
    messages: tuple[AgentMessage, ...]
    model_calls: tuple[ModelCallRecord, ...]
    actions: tuple[StoredAction, ...]
    approvals: tuple[ActionApproval, ...] = ()
    reconciliations: tuple[ActionReconciliation, ...] = ()
    model_attempts: tuple[ModelAttemptRecord, ...] = ()
    provider_key: ProviderKey | None = None
    model_permits: tuple[ModelPermit, ...] = ()
    observed_usage: ObservedUsageReport | None = None

    def usage_summary(self) -> ModelUsageSummary:
        return summarize_usage(self.model_attempts)


class AgentStore(Protocol):
    def usage_summary(self, session_id: str) -> ModelUsageSummary:
        """Read persisted facts, including terminal sessions; caller controls access."""
        ...

    def request_action_approval(
        self, task: Task, action_id: str, expected_revision: int
    ) -> AgentSession: ...

    def decide_action(
        self, approval_id: str, decision: ApprovalDecision
    ) -> ActionApproval: ...

    def reconcile_action(
        self, session_id: str, action_id: str, decision: ReconciliationDecision
    ) -> ActionReconciliation: ...

    def open(
        self,
        task: Task,
        initial_snapshot: AgentSnapshot,
        *,
        messages: tuple[AgentMessage, ...] = (),
    ) -> AgentSession: ...

    def load(self, task: Task) -> AgentSession: ...

    def commit_transition(
        self, task: Task, transition: AgentTransition
    ) -> AgentSession: ...

    def complete_agent_step(
        self, task: Task, transition: AgentTransition, output: JsonValue
    ) -> AgentSession: ...

    def fail_agent_step(
        self, task: Task, transition: AgentTransition
    ) -> AgentSession: ...

    def suspend_agent_step(
        self, task: Task, transition: AgentTransition
    ) -> AgentSession: ...

    def defer_model_retry(
        self, task: Task, transition: AgentTransition, retry_delay_seconds: float
    ) -> AgentSession:
        """确认模型失败并原子释放租约；延迟 (0, 3600] 秒按数据库时钟结算。"""
        ...
