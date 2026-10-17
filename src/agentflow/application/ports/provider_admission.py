"""Trusted deployment policy and durable model reservation, never workflow input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agentflow.domain.agent.action import Identifier
from agentflow.domain.execution.task import Task

if TYPE_CHECKING:
    from agentflow.application.ports.agent_store import AgentSession, AgentTransition

ProviderKey = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")]
MAX_COOLDOWN_SECONDS = 3600


class ProviderAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_key: ProviderKey
    max_concurrent_requests: int = Field(ge=1, le=1_000_000, strict=True)
    max_requests_per_window: int = Field(ge=1, le=1_000_000, strict=True)
    window_seconds: float = Field(ge=1, le=86400, allow_inf_nan=False, strict=True)
    admission_retry_delay_seconds: float = Field(
        default=1, ge=0.1, le=3600, allow_inf_nan=False, strict=True
    )
    permit_ttl_seconds: float = Field(
        default=120, ge=1, le=86400, allow_inf_nan=False, strict=True
    )
    policy_version: str = Field(min_length=1, max_length=80, pattern=r"^[\w.-]+$")


class ProviderBackpressure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["rate_limited"] = "rate_limited"
    retry_after_seconds: float = Field(
        default=0, ge=0, le=MAX_COOLDOWN_SECONDS, allow_inf_nan=False, strict=True
    )


class ModelPermit(BaseModel):
    """Read-only permit history. The worker token/digest is never exposed here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    provider_key: ProviderKey
    session_id: Identifier
    model_call_id: Identifier
    attempt_index: int = Field(ge=1, strict=True)
    reservation_revision: int = Field(ge=1, strict=True)
    status: Literal["active", "released", "unknown"]
    created_at: float = Field(ge=0, allow_inf_nan=False)
    expires_at: float = Field(ge=0, allow_inf_nan=False)
    released_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class ModelAdmission:
    state: AgentSession
    status: Literal["reserved", "admission_deferred"]
    permit: ModelPermit | None = None


class ProviderAdmissionStore(Protocol):
    def reserve_model_attempt(
        self, task: Task, transition: AgentTransition
    ) -> ModelAdmission:
        """Commit permit + attempt, or unchanged budget + durable ready deferral."""
        ...

    def validate_model_dispatch(self, task: Task, permit: ModelPermit) -> None:
        """Check current ownership and unexpired permit immediately before HTTP."""
        ...
