"""Soft limits on persisted, version-mapped observations, never a billing cap."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentflow.application.ports.model_usage import (
    MappingName,
    ModelUsageSummary,
    UsageMapping,
    summarize_usage,
)
from agentflow.domain.agent.budget import Counter

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


class ObservedUsagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_observed_input_tokens: Counter | None = None
    max_observed_output_tokens: Counter | None = None
    max_observed_total_tokens: Counter | None = None
    missing_usage_policy: Literal["fail_closed", "continue_with_warning"] = (
        "fail_closed"
    )
    policy_version: MappingName


class UsageGovernanceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deployment_policy: ObservedUsagePolicy | None = None
    usage_mappings: tuple[UsageMapping, ...] | None = None


class ObservedUsageReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: ObservedUsagePolicy
    summary: ModelUsageSummary
    governance: Literal["observed_soft_limit"] = "observed_soft_limit"
    partial: bool
    blocked: bool
    unversioned_attempts: Counter
    error_code: Literal["AgentObservedUsageLimit", "AgentUsageUnavailable"] | None = (
        None
    )


def stricter_policy(deployment, workflow):
    if deployment is None:
        return workflow
    if workflow is None:
        return deployment
    limits = {}
    for field in TOKEN_FIELDS:
        name = f"max_observed_{field}"
        values = [
            v for p in (deployment, workflow) if (v := getattr(p, name)) is not None
        ]
        limits[name] = min(values) if values else None
    return ObservedUsagePolicy(
        **limits,
        missing_usage_policy="fail_closed"
        if "fail_closed"
        in {
            deployment.missing_usage_policy,
            workflow.missing_usage_policy,
        }
        else "continue_with_warning",
        # Both original versions remain in the Run binding and immutable StepSpec.
        policy_version="intersection-v1",
    )


def assess_usage(attempts, policy) -> ObservedUsageReport:
    summary = summarize_usage(attempts, verified_only=True)
    required = [
        (getattr(policy, f"max_observed_{field}"), getattr(summary, field))
        for field in TOKEN_FIELDS
        if getattr(policy, f"max_observed_{field}") is not None
    ]
    partial = any(
        getattr(summary, field).unreported_attempts > 0 for field in TOKEN_FIELDS
    )
    reached = any(
        limit == 0 or (counter.value is not None and counter.value >= limit)
        for limit, counter in required
    )
    missing = (
        any(counter.unreported_attempts > 0 for _, counter in required)
        and policy.missing_usage_policy == "fail_closed"
    )
    return ObservedUsageReport(
        policy=policy,
        summary=summary,
        partial=partial,
        blocked=reached or missing,
        unversioned_attempts=sum(a.usage.mapping is None for a in attempts),
        error_code="AgentObservedUsageLimit"
        if reached
        else "AgentUsageUnavailable"
        if missing
        else None,
    )
