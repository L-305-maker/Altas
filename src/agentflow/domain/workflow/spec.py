"""持久化工作流的声明格式。

V0.1 的 Python callable 不能安全地存进数据库。这里保存注册名而非 pickle，
worker 只允许执行部署时注册过的函数；来自 API 的 JSON 不能指定任意导入路径。
"""

from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Name = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")]


class StepSpec(BaseModel):
    """重试上限包含第一次执行；超时以秒计，审批发生在实际调用之前。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Name
    handler: Name
    depends_on: tuple[Name, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_delay: float = Field(default=1, ge=0, le=3600, allow_inf_nan=False)
    timeout: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    requires_approval: bool = False
    artifact: bool = False


class WorkflowSpec(BaseModel):
    """同名同版本不可覆盖；每次运行还会复制一份定义快照以支持恢复。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Name
    version: int = Field(default=1, ge=1)
    steps: tuple[StepSpec, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        ids = {step.id for step in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("step ids must be unique")
        for step in self.steps:
            if set(step.depends_on) - ids:
                raise ValueError(f"unknown dependency for {step.id}")
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError(f"duplicate dependency for {step.id}")
        try:
            tuple(
                TopologicalSorter(
                    {s.id: s.depends_on for s in self.steps}
                ).static_order()
            )
        except CycleError as error:
            raise ValueError("workflow contains a cycle") from error
        return self
