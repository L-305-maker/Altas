"""Agent 状态迁移只判断领域合法性；并发所有权由持久化层检查。"""

from enum import StrEnum
from types import MappingProxyType

from agentflow.domain.execution.exception import InvalidStateTransitionError


class AgentStatus(StrEnum):
    READY_MODEL = "ready_model"
    MODEL_INFLIGHT = "model_inflight"
    ACTIONS_PENDING = "actions_pending"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_RECONCILIATION = "waiting_reconciliation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AgentStatus.SUCCEEDED,
            AgentStatus.FAILED,
            AgentStatus.EXHAUSTED,
            AgentStatus.CANCELLED,
        }


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    INFLIGHT = "inflight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DENIED = "denied"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.DENIED,
        }


class ToolSideEffect(StrEnum):
    """工具声明的副作用语义，不构成对 UNKNOWN 的重试授权。

    READ_ONLY 不修改外部状态；IDEMPOTENT 重复同一操作不增加副作用；
    RECONCILABLE 可查询外部事实以对账；UNSAFE 需要人工/工具专属处置。
    """

    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    UNSAFE = "unsafe"


_AGENT_TRANSITIONS = MappingProxyType(
    {
        AgentStatus.READY_MODEL: frozenset({AgentStatus.MODEL_INFLIGHT}),
        AgentStatus.MODEL_INFLIGHT: frozenset(
            {
                AgentStatus.READY_MODEL,
                AgentStatus.ACTIONS_PENDING,
                AgentStatus.SUCCEEDED,
            }
        ),
        AgentStatus.ACTIONS_PENDING: frozenset(
            {
                AgentStatus.READY_MODEL,
                AgentStatus.WAITING_APPROVAL,
                AgentStatus.WAITING_RECONCILIATION,
            }
        ),
        AgentStatus.WAITING_APPROVAL: frozenset({AgentStatus.ACTIONS_PENDING}),
        AgentStatus.WAITING_RECONCILIATION: frozenset({AgentStatus.ACTIONS_PENDING}),
    }
)


def transition_agent(current: AgentStatus, target: AgentStatus) -> AgentStatus:
    """已确认的模型失败可回到 READY_MODEL；不隐含重试或预算消耗。"""
    current, target = AgentStatus(current), AgentStatus(target)
    if not current.is_terminal and (
        target in _AGENT_TRANSITIONS[current]
        or target in {AgentStatus.FAILED, AgentStatus.EXHAUSTED, AgentStatus.CANCELLED}
    ):
        return target
    raise InvalidStateTransitionError(f"agent: {current} -> {target}")
