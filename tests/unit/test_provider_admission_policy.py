import pytest
from pydantic import ValidationError

from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy


def policy(**changes):
    return ProviderAdmissionPolicy(
        **{
            "provider_key": "deployment-model-a",
            "max_concurrent_requests": 2,
            "max_requests_per_window": 10,
            "window_seconds": 60,
            "admission_retry_delay_seconds": 1,
            "permit_ttl_seconds": 120,
            "policy_version": "1",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_key": "https://private:key@host"},
        {"max_concurrent_requests": 0},
        {"max_concurrent_requests": True},
        {"max_requests_per_window": 1.5},
        {"window_seconds": float("inf")},
        {"admission_retry_delay_seconds": 0.000001},
        {"permit_ttl_seconds": -1},
        {"policy_version": ""},
    ],
)
def test_admission_policy_is_bounded_and_strict(changes):
    with pytest.raises(ValidationError):
        policy(**changes)


def test_admission_policy_roundtrips_without_secrets():
    value = policy()
    assert ProviderAdmissionPolicy.model_validate_json(value.model_dump_json()) == value
