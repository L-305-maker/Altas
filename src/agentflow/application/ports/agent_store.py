"""Durable Agent 的事务契约；不依赖 SQLAlchemy 或模型供应商。

消息序号从 0 开始连续追加。assistant payload 的 action_ids 对应有序批次；
工具观察由 Store 根据 observations 生成，调用方不得伪造 tool 消息。
所有写入先持久化预约，再由调用方在事务外执行外部操作。
"""

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentflow.domain.agent.action import Action, Identifier
from agentflow.domain.agent.budget import Counter
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


class AgentTransition(BaseModel):
    """一次原子提交：快照、消息、模型结果、参数及工具观察。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: AgentSnapshot
    messages: tuple[AgentMessage, ...] = ()
    model_result: ModelCallResult | None = None
    arguments: tuple[ActionArguments, ...] = ()
    observations: tuple[ActionObservation, ...] = ()


class AgentSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: AgentSnapshot
    messages: tuple[AgentMessage, ...]
    model_calls: tuple[ModelCallRecord, ...]
    actions: tuple[StoredAction, ...]


class AgentStore(Protocol):
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
