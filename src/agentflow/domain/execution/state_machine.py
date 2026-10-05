"""显式迁移表避免散落的 if/else。

状态机是纯校验服务，只返回目标状态，不执行步骤、不访问数据库。
持久执行层还需要在事务内核对租约和令牌：仅验证状态无法防止并发覆盖。
"""

from agentflow.domain.execution.exception import InvalidStateTransitionError
from agentflow.domain.execution.status import StepStatus, WorkflowStatus

STEP_TRANSITIONS = {
    StepStatus.PENDING: {
        StepStatus.READY,
        StepStatus.CANCELLED,
    },
    StepStatus.READY: {
        StepStatus.RUNNING,
        StepStatus.CANCELLED,
    },
    StepStatus.RUNNING: {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.WAITING,
        StepStatus.CANCELLED,
    },
    StepStatus.WAITING: {
        StepStatus.READY,
        StepStatus.CANCELLED,
    },
    StepStatus.FAILED: {
        StepStatus.RETRYING,
        StepStatus.CANCELLED,
    },
    StepStatus.RETRYING: {
        StepStatus.RUNNING,
    },
    StepStatus.SUCCEEDED: set(),
    StepStatus.CANCELLED: set(),
}


class StepStateMachine:
    def transition(self, current: StepStatus, target: StepStatus) -> StepStatus:
        allowed = STEP_TRANSITIONS[current]
        if target not in allowed:
            raise InvalidStateTransitionError()
        return target


WORKFLOW_TRANSITION = {
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.CANCELLED: set(),
    WorkflowStatus.SUCCEEDED: set(),
    WorkflowStatus.RUNNING: {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.WAITING,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.PENDING: {WorkflowStatus.CANCELLED, WorkflowStatus.RUNNING},
    WorkflowStatus.WAITING: {WorkflowStatus.CANCELLED, WorkflowStatus.RUNNING},
}


class WorkflowStateMachine:
    def transition(
        self, current: WorkflowStatus, target: WorkflowStatus
    ) -> WorkflowStatus:
        allowed = WORKFLOW_TRANSITION[current]
        if target not in allowed:
            raise InvalidStateTransitionError()
        return target
