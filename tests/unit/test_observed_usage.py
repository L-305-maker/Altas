import pytest
from pydantic import ValidationError

from agentflow.application.ports.agent_store import ModelAttemptRecord
from agentflow.application.ports.model_usage import (
    DEEPSEEK_USAGE_MAPPING,
    ModelUsage,
    UsageMapping,
    UsageMappingRegistry,
)
from agentflow.application.ports.observed_usage import (
    ObservedUsagePolicy,
    assess_usage,
    stricter_policy,
)


def policy(**values):
    return ObservedUsagePolicy(policy_version="1", **values)


def attempt(raw=None, **values):
    return ModelAttemptRecord(
        session_id="session",
        model_call_id="call",
        attempt_index=1,
        outcome="succeeded",
        created_at=1,
        confirmed_at=2,
        raw_usage=raw,
        usage=UsageMappingRegistry().normalize(raw),
        **values,
    )


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", float("inf")])
def test_observed_thresholds_are_strict(value):
    with pytest.raises(ValidationError):
        policy(max_observed_input_tokens=value)


def test_stricter_policy_merges_limits_and_missing_rules():
    deployment = policy(max_observed_input_tokens=10)
    workflow = policy(
        max_observed_input_tokens=20,
        max_observed_output_tokens=5,
        missing_usage_policy="continue_with_warning",
    )
    merged = stricter_policy(deployment, workflow)
    assert merged.max_observed_input_tokens == 10
    assert merged.max_observed_output_tokens == 5
    assert merged.missing_usage_policy == "fail_closed"
    assert stricter_policy(None, None) is None


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
def test_threshold_is_inclusive_and_zero_prevents_first_reservation(field):
    threshold = {f"max_observed_{field}": 5}
    assert not assess_usage([attempt({field: 4})], policy(**threshold)).blocked
    assert assess_usage([attempt({field: 5})], policy(**threshold)).blocked
    assert assess_usage([], policy(**{f"max_observed_{field}": 0})).blocked
    assert not assess_usage([], policy(**threshold)).blocked


def test_partial_usage_never_infers_total_and_warning_keeps_coverage():
    records = [attempt({"input_tokens": 4, "output_tokens": 2})]
    closed = assess_usage(records, policy(max_observed_total_tokens=10))
    assert closed.blocked and closed.error_code == "AgentUsageUnavailable"
    assert closed.summary.total_tokens.value is None
    warning = assess_usage(
        records,
        policy(
            max_observed_total_tokens=10, missing_usage_policy="continue_with_warning"
        ),
    )
    assert warning.partial and not warning.blocked
    assert warning.summary.total_tokens.unreported_attempts == 1


def test_legacy_unknown_and_unversioned_measurements_are_not_verified_zero():
    records = [
        ModelAttemptRecord(
            session_id="s",
            model_call_id="a",
            attempt_index=1,
            outcome="unknown",
            created_at=1,
            confirmed_at=2,
        ),
        ModelAttemptRecord(
            session_id="s",
            model_call_id="b",
            attempt_index=1,
            outcome="LEGACY_UNMEASURED",
            accounting="LEGACY_UNMEASURED",
        ),
        ModelAttemptRecord(
            session_id="s",
            model_call_id="c",
            attempt_index=1,
            outcome="succeeded",
            created_at=1,
            confirmed_at=2,
            raw_usage={"total_tokens": 3},
            usage=ModelUsage(total_tokens=3, source="provider_reported"),
        ),
    ]
    result = assess_usage(records, policy(max_observed_total_tokens=10))
    assert result.blocked and result.summary.total_tokens.value is None
    assert result.summary.total_tokens.unreported_attempts == 3
    assert records[-1].usage.total_tokens == 3


def test_mapping_is_versioned_registered_and_matches_raw_measurements():
    registry = UsageMappingRegistry()
    raw = {"prompt_tokens": 4, "completion_tokens": 2, "cache": {"hit": 1}}
    mapped = DEEPSEEK_USAGE_MAPPING.normalize(raw)
    assert registry.normalize(raw, mapped) == mapped
    assert mapped.mapping.provider == "deepseek" and mapped.mapping.version == "1"
    assert mapped.total_tokens is None
    conflicting = ModelUsage(input_tokens=9, source="provider_reported")
    with pytest.raises(ValueError):
        registry.normalize({"input_tokens": 4}, conflicting)
    custom = UsageMapping(
        provider="custom", version="1", input_field="in", output_field="out"
    )
    with pytest.raises(ValueError):
        registry.normalize({"in": 4}, custom.normalize({"in": 4}))
    assert (
        UsageMappingRegistry((custom,))
        .normalize({"in": 4}, custom.normalize({"in": 4}))
        .input_tokens
        == 4
    )


def test_missing_unlimited_axes_warn_without_blocking_complete_required_axis():
    result = assess_usage(
        [attempt({"input_tokens": 1})], policy(max_observed_input_tokens=5)
    )
    assert result.partial and not result.blocked


def test_mapping_version_cannot_change_rules_and_registrations_are_immutable():
    definition = UsageMapping(provider="custom", version="1", input_field="in")
    registrations = [definition]
    registry = UsageMappingRegistry(registrations)
    registrations.clear()
    changed = UsageMapping(provider="custom", version="1", input_field="other")
    with pytest.raises(ValueError):
        registry.normalize({"other": 3}, changed.normalize({"other": 3}))
    assert (
        registry.normalize({"in": 3}, definition.normalize({"in": 3})).input_tokens == 3
    )
