"""可恢复的累积预算。调用方在派发前预留，资源按约定的整数单位计量。"""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Counter = Annotated[int, Field(ge=0, strict=True)]
_RESOURCES = ("model_rounds", "model_attempts", "tool_calls", "resource_units")


class AgentBudgetExhaustedError(Exception):
    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(f"agent {resource} budget exhausted")


class AgentBudget(BaseModel):
    """模型轮次是逻辑调用；尝试数含首次调用及重试，工具数按实际派发计。

    达到上限的本次消耗合法，下一次正消耗才耗尽；零上限禁止该类消耗。
    资源单位由适配层定义（例如 token 或微单位费用），整个执行期间不变。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_rounds: Counter
    max_model_attempts: Counter
    max_tool_calls: Counter
    max_resource_units: Counter
    model_rounds: Counter = 0
    model_attempts: Counter = 0
    tool_calls: Counter = 0
    resource_units: Counter = 0

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        if self.model_attempts < self.model_rounds:
            raise ValueError("each model round requires at least one attempt")
        for resource in _RESOURCES:
            if getattr(self, resource) > getattr(self, f"max_{resource}"):
                raise ValueError(f"{resource} usage exceeds its limit")
        return self

    def consume(
        self,
        *,
        model_rounds: int = 0,
        model_attempts: int = 0,
        tool_calls: int = 0,
        resource_units: int = 0,
    ) -> Self:
        """原子地返回新预算；无退款、隐式重置或对原对象的部分修改。"""
        charges = dict(
            zip(
                _RESOURCES,
                (model_rounds, model_attempts, tool_calls, resource_units),
                strict=True,
            )
        )
        if any(type(value) is not int or value < 0 for value in charges.values()):
            raise ValueError("budget charges must be nonnegative integers")
        data = self.model_dump()
        for resource, amount in charges.items():
            data[resource] += amount
            if data[resource] > getattr(self, f"max_{resource}"):
                raise AgentBudgetExhaustedError(resource)
        return type(self).model_validate(data)
