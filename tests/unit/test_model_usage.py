import pytest
from pydantic import ValidationError

from agentflow.application.ports.model_usage import ModelUsage, normalize_usage


@pytest.mark.parametrize("invalid", [-1, 1.5, True, "2", float("inf"), float("nan")])
def test_usage_requires_strict_nonnegative_integer(invalid):
    with pytest.raises(ValidationError):
        ModelUsage(input_tokens=invalid, source="provider_reported")
    usage = normalize_usage({"input_tokens": invalid, "output_tokens": 3})
    assert usage.input_tokens is None and usage.output_tokens == 3
    assert usage.total_tokens is None  # Never infer a missing total.


def test_missing_usage_is_not_zero_and_reported_zero_is_known():
    assert normalize_usage(None) == ModelUsage()
    assert normalize_usage({"extra": {"cache": 4}}) == ModelUsage()
    assert normalize_usage({"input_tokens": 0}) == ModelUsage(
        input_tokens=0, source="provider_reported"
    )
    with pytest.raises(ValidationError):
        ModelUsage(input_tokens=1, source="unavailable")


def test_provider_field_mapping_does_not_infer_missing_values():
    usage = normalize_usage(
        {"prompt_tokens": 11, "completion_tokens": 4, "cache_hit_tokens": 8},
        input_field="prompt_tokens",
        output_field="completion_tokens",
    )
    assert usage == ModelUsage(
        input_tokens=11, output_tokens=4, source="provider_reported"
    )
