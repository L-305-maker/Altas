"""Provider-reported measurements, not independently verified billing or hard caps."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from agentflow.domain.agent.budget import Counter

MAX_USAGE_BYTES = 16_000


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: Counter | None = None
    output_tokens: Counter | None = None
    total_tokens: Counter | None = None
    source: Literal["provider_reported", "unavailable"] = "unavailable"

    @model_validator(mode="after")
    def measured_source(self) -> Self:
        known = any(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.total_tokens)
        )
        if known != (self.source == "provider_reported"):
            raise ValueError("usage source must agree with reported measurements")
        return self


def normalize_usage(
    raw: dict | None,
    *,
    input_field: str = "input_tokens",
    output_field: str = "output_tokens",
) -> ModelUsage:
    """Map explicit fields only; invalid or absent measurements remain unknown."""
    if not isinstance(raw, dict):
        return ModelUsage()
    values = {}
    for target, key in (
        ("input_tokens", input_field),
        ("output_tokens", output_field),
        ("total_tokens", "total_tokens"),
    ):
        value = (raw or {}).get(key)
        values[target] = value if type(value) is int and value >= 0 else None
    return ModelUsage(
        **values,
        source="provider_reported"
        if any(v is not None for v in values.values())
        else "unavailable",
    )


class TokenUsageTotal(BaseModel):
    """Partial sum with coverage; None means no measurement, never inferred zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Counter | None
    reported_attempts: Counter
    unreported_attempts: Counter


class ModelUsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    attempts: Counter
    inflight_attempts: Counter
    unknown_outcomes: Counter
    legacy_unmeasured_attempts: Counter
    unavailable_usage_attempts: Counter
    input_tokens: TokenUsageTotal
    output_tokens: TokenUsageTotal
    total_tokens: TokenUsageTotal
