"""worker 与持久化层之间传递的值对象，不携带 SQLAlchemy session。"""

from dataclasses import dataclass
from typing import Any

from agentflow.domain.workflow.spec import StepSpec


@dataclass(frozen=True)
class Task:
    run_id: str
    step_id: str
    token: str
    attempt: int
    spec: StepSpec
    inputs: dict[str, Any]
    dependencies: dict[str, Any]

    @property
    def idempotency_key(self) -> str:
        """重试保持不变，外部有副作用的工具应使用它实现去重。"""
        return f"{self.run_id}:{self.step_id}"
