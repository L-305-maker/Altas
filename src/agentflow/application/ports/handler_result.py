"""已经通过 AgentStore 原子提交的结果；Worker 不应再次完成此步骤。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CommittedAgentResult:
    run_id: str
    step_id: str
    token: str
    status: Literal[
        "succeeded",
        "failed",
        "exhausted",
        "waiting_approval",
        "waiting_reconciliation",
        "retry_scheduled",
        "admission_deferred",
    ]
    revision: int

    def __post_init__(self):
        if (
            self.status
            not in {
                "succeeded",
                "failed",
                "exhausted",
                "waiting_approval",
                "waiting_reconciliation",
                "retry_scheduled",
                "admission_deferred",
            }
            or type(self.revision) is not int
            or self.revision < 1
            or not self.run_id
            or not self.step_id
            or not self.token
        ):
            raise ValueError("invalid committed agent result")
