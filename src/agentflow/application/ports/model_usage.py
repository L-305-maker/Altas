"""Provider-reported measurements, not independently verified billing or hard caps."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.domain.agent.budget import Counter

MAX_USAGE_BYTES = 16_000
MappingName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]


class UsageMapping(BaseModel):
    """A trusted, versioned top-level field mapping; never infers token counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: MappingName
    version: MappingName
    input_field: str = Field(default="input_tokens", min_length=1, max_length=80)
    output_field: str = Field(default="output_tokens", min_length=1, max_length=80)
    total_field: str = Field(default="total_tokens", min_length=1, max_length=80)

    def normalize(self, raw):
        usage = normalize_usage(
            raw,
            input_field=self.input_field,
            output_field=self.output_field,
            total_field=self.total_field,
        )
        return ModelUsage(
            **usage.model_dump(exclude={"mapping"}),
            mapping=self if raw is not None else None,
        )


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: Counter | None = None
    output_tokens: Counter | None = None
    total_tokens: Counter | None = None
    source: Literal["provider_reported", "unavailable"] = "unavailable"
    mapping: UsageMapping | None = None

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
    total_field: str = "total_tokens",
) -> ModelUsage:
    """Map explicit fields only; invalid or absent measurements remain unknown."""
    if not isinstance(raw, dict):
        return ModelUsage()
    values = {}
    for target, key in (
        ("input_tokens", input_field),
        ("output_tokens", output_field),
        ("total_tokens", total_field),
    ):
        value = (raw or {}).get(key)
        values[target] = value if type(value) is int and value >= 0 else None
    return ModelUsage(
        **values,
        source="provider_reported"
        if any(v is not None for v in values.values())
        else "unavailable",
    )


STANDARD_USAGE_MAPPING = UsageMapping(provider="generic", version="1")
DEEPSEEK_USAGE_MAPPING = UsageMapping(
    provider="deepseek",
    version="1",
    input_field="prompt_tokens",
    output_field="completion_tokens",
)


@dataclass(frozen=True)
class UsageMappingRegistry:
    """Deployment registrations only. Retain old versions for history recovery."""

    custom: tuple[UsageMapping, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self,
            "custom",
            tuple(UsageMapping.model_validate(m.model_dump()) for m in self.custom),
        )
        definitions = self.definitions
        if len({(m.provider, m.version) for m in definitions}) != len(definitions):
            raise ValueError("usage mapping identities must be unique")

    @property
    def definitions(self):
        return tuple(
            sorted(
                (STANDARD_USAGE_MAPPING, DEEPSEEK_USAGE_MAPPING, *self.custom),
                key=lambda m: (m.provider, m.version),
            )
        )

    def normalize(self, raw, supplied: ModelUsage | None = None) -> ModelUsage:
        mapping = (
            supplied.mapping
            if supplied and supplied.mapping
            else STANDARD_USAGE_MAPPING
        )
        if mapping not in self.definitions:
            raise ValueError("usage mapping is not registered or changed version rules")
        expected = mapping.normalize(raw)
        if supplied is not None and supplied.model_dump(
            exclude={"mapping"}
        ) != expected.model_dump(exclude={"mapping"}):
            raise ValueError("normalized usage conflicts with raw provider fields")
        return expected


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


def summarize_usage(attempts, *, verified_only=False) -> ModelUsageSummary:
    totals = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        values = [
            value
            for attempt in attempts
            if (not verified_only or attempt.usage.mapping is not None)
            and (value := getattr(attempt.usage, field)) is not None
        ]
        totals[field] = TokenUsageTotal(
            value=sum(values) if values else None,
            reported_attempts=len(values),
            unreported_attempts=len(attempts) - len(values),
        )
    return ModelUsageSummary(
        attempts=len(attempts),
        inflight_attempts=sum(a.outcome == "inflight" for a in attempts),
        unknown_outcomes=sum(a.outcome == "unknown" for a in attempts),
        legacy_unmeasured_attempts=sum(
            a.accounting == "LEGACY_UNMEASURED" for a in attempts
        ),
        unavailable_usage_attempts=sum(
            a.usage.source == "unavailable" for a in attempts
        ),
        **totals,
    )
