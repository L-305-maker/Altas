"""标识值对象：不同类型的 ID 即使字符串相同也不相等。

frozen=True 使其可哈希且不可修改，适合作为 Graph 字典的键，避免误用
WorkflowId 访问 StepId 索引。它们不负责生成 ID，生成策略属于应用层。
"""

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
