"""供未来 AgentStore 使用的不可变快照，不包含租约、数据库或运行时行为。"""

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from agentflow.domain.agent.action import Action, Identifier
from agentflow.domain.agent.budget import AgentBudget, Counter
from agentflow.domain.agent.status import ActionStatus, AgentStatus, transition_agent


class AgentSnapshot(BaseModel):
    """actions 只包含最近一轮模型调用的批次；历史由持久化层另行保留。

    advance 要求候选 revision 恰好加一；Store 用旧 revision 执行 CAS，
    在同一事务检查有效租约及 fencing token。快照本身不授予执行所有权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    budget: AgentBudget
    revision: Counter = 0
    status: AgentStatus = AgentStatus.READY_MODEL
    model_call_id: Identifier | None = None
    actions: tuple[Action, ...] = ()

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        if (self.model_call_id is not None) != (self.budget.model_rounds > 0):
            raise ValueError("model_call_id and charged model rounds must agree")
        if len({action.id for action in self.actions}) != len(self.actions):
            raise ValueError("action ids must be unique")
        if any(
            action.agent_id != self.id or action.model_call_id != self.model_call_id
            for action in self.actions
        ):
            raise ValueError("actions must belong to this agent and model call")
        executed = sum(
            action.status not in {ActionStatus.PROPOSED, ActionStatus.DENIED}
            for action in self.actions
        )
        if executed > self.budget.tool_calls:
            raise ValueError("executed actions require charged tool calls")
        if self.status in {AgentStatus.MODEL_INFLIGHT, AgentStatus.SUCCEEDED}:
            if self.model_call_id is None or self.actions:
                raise ValueError("model execution requires a call id and no actions")
        if (
            self.status
            in {
                AgentStatus.ACTIONS_PENDING,
                AgentStatus.WAITING_APPROVAL,
                AgentStatus.WAITING_RECONCILIATION,
            }
            and not self.actions
        ):
            raise ValueError("action states require a nonempty batch")
        if self.status == AgentStatus.READY_MODEL and any(
            not action.status.is_terminal for action in self.actions
        ):
            raise ValueError("all actions must be resolved before the next model call")
        unknown = any(action.status == ActionStatus.UNKNOWN for action in self.actions)
        if unknown and not (
            self.status.is_terminal or self.status == AgentStatus.WAITING_RECONCILIATION
        ):
            raise ValueError("unknown actions require reconciliation")
        if self.status == AgentStatus.WAITING_RECONCILIATION and not unknown:
            raise ValueError("reconciliation requires an unknown action")
        if self.status == AgentStatus.WAITING_APPROVAL:
            statuses = {action.status for action in self.actions}
            if (
                ActionStatus.PROPOSED not in statuses
                or ActionStatus.INFLIGHT in statuses
            ):
                raise ValueError("approval requires proposals and no inflight actions")
        return self

    def advance(self, candidate: Self) -> Self:
        """校验完整候选快照；同状态写入用于 Action 进展及预算预留。

        创建/恢复时的结构校验不能证明历史合法性，写入前还须调用此方法。
        UNKNOWN 的结果变更是显式对账；此方法不判断外部证据是否充分。
        """
        if self.status.is_terminal or candidate.status != self.status:
            transition_agent(self.status, candidate.status)
        if candidate.id != self.id or candidate.revision != self.revision + 1:
            raise ValueError("successor must preserve agent id and increment revision")
        charges = {
            resource: getattr(candidate.budget, resource)
            - getattr(self.budget, resource)
            for resource in (
                "model_rounds",
                "model_attempts",
                "tool_calls",
                "resource_units",
            )
        }
        if self.budget.consume(**charges) != candidate.budget:
            raise ValueError("budget limits cannot change during an execution")
        starts_model = (
            self.status == AgentStatus.READY_MODEL
            and candidate.status == AgentStatus.MODEL_INFLIGHT
        )
        if self.model_call_id != candidate.model_call_id:
            if not starts_model or charges["model_rounds"] != 1:
                raise ValueError("a new model call requires a new model round")
            if charges["model_attempts"] < 1:
                raise ValueError("a new model call requires an attempt")
            return candidate
        if charges["model_rounds"]:
            raise ValueError("a new model round requires a new call id")
        if starts_model and charges["model_attempts"] < 1:
            raise ValueError("a model retry requires a charged attempt")
        if (
            self.status == AgentStatus.MODEL_INFLIGHT
            and candidate.status == AgentStatus.ACTIONS_PENDING
        ):
            if any(a.status != ActionStatus.PROPOSED for a in candidate.actions):
                raise ValueError("model responses may only propose actions")
            return candidate
        previous = {action.id: action for action in self.actions}
        if previous.keys() != {action.id for action in candidate.actions}:
            raise ValueError("actions cannot be added or removed within a model batch")
        dispatched = 0
        for action in candidate.actions:
            old = previous[action.id]
            if old == action:
                continue
            if (
                old.status == ActionStatus.PROPOSED
                and action.status == ActionStatus.INFLIGHT
            ):
                dispatched += 1
            if old.status == ActionStatus.UNKNOWN:
                expected = old.reconcile(action.status, error_code=action.error_code)
            else:
                expected = old.transition(action.status, error_code=action.error_code)
            if action != expected:
                raise ValueError(
                    "action identity and invocation metadata are immutable"
                )
        if charges["tool_calls"] < dispatched:
            raise ValueError("each new action dispatch requires a charged tool call")
        return candidate
