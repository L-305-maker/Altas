"""已经通过 AgentStore 原子提交的结果；Worker 不应再次完成此步骤。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CommittedAgentResult:
    run_id: str
    step_id: str
    token: str
    status: Literal[
        "succeeded", "failed", "exhausted", "waiting_approval", "waiting_reconciliation"
    ]
    revision: int
