"""进程内定义与运行实例分离：定义描述“做什么”，Run 描述“这次做到了哪里”。

此处的 callable 仅适用于本地模式。持久模式使用 spec.py 的注册名和 JSON 参数，
不将 Python 函数、闭包或可执行 pickle 写入数据库。
"""

from collections.abc import Callable
from dataclasses import dataclass

from agentflow.domain.workflow.ids import StepId, WorkflowId


@dataclass(frozen=True)
class StepDefinition:
    id: StepId
    name: str
    handler: Callable[[], None]


@dataclass(frozen=True)
class WorkflowDefinition:
    id: WorkflowId
    name: str
