from dataclasses import dataclass
from collections.abc import Callable

from agentflow.domain.workflow.ids import StepId,WorkflowId


@dataclass(frozen=True)
class StepDefinition:
    id: StepId
    name: str
    handler: Callable[[], None]

@dataclass(frozen=True)
class WorkflowDefinition:
    id: WorkflowId
    name: str