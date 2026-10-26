"""Trusted deployment capacity; independent of Worker slots and Provider permits."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_version: Literal["v1"] = "v1"
    max_active_runs: int | None = Field(default=None, ge=1, strict=True)


class WorkflowCapacityExceeded(Exception):
    def __init__(self):
        super().__init__("WorkflowCapacityExceeded")
