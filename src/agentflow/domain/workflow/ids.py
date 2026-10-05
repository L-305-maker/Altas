from dataclasses import dataclass

@dataclass(frozen=True)
class WorkflowId:
    value: str

@dataclass(frozen=True)
class StepId:
    value: str

@dataclass(frozen=True)
class WorkflowRunId:
    value: str

@dataclass(frozen=True)
class StepRunId:
    value: str