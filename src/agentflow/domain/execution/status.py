"""状态使用 StrEnum，使日志可读；合法的状态迁移由 state_machine.py 约束。

枚举包含后续等待/重试状态，不代表每个执行器都实现全部状态。
持久队列的延迟重试表示为 ready + available_at，并在事件中记录 retrying。
"""

from enum import StrEnum


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
