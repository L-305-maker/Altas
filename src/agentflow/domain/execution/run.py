from dataclasses import dataclass, field

from agentflow.domain.execution.status import StepStatus, WorkflowStatus
from agentflow.domain.workflow.ids import StepId, StepRunId, WorkflowId, WorkflowRunId


@dataclass
class StepRun:
    id: StepRunId
    step_id: StepId
    status: StepStatus
    error: Exception | None = None


@dataclass
class WorkflowRun:
    id: WorkflowRunId
    workflow_id: WorkflowId
    status: WorkflowStatus
    step_runs: dict[StepId, StepRun] = field(default_factory=dict)
