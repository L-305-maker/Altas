import pytest
from pydantic import ValidationError


def test_consumption_policy_is_closed_strict_and_frozen():
    from agentflow.application.ports.summary_consumption import SummaryConsumptionPolicy

    policy = SummaryConsumptionPolicy()
    assert policy.mode == "disabled" and not policy.allow_claim_text
    with pytest.raises(ValidationError):
        policy.allow_claim_text = True
    for changes in (
        {"max_consumed_claims": True},
        {"allow_claim_text": "yes"},
        {"minimum_selected_evidence_coverage": float("nan")},
        {"max_consumption_bytes": 4097},
        {"source_session": "forged"},
    ):
        with pytest.raises(ValidationError):
            SummaryConsumptionPolicy(**changes)


def test_scoped_tool_registration_preserves_runtime_authorization():
    from agentflow.application.agents.summary_consumption import consumption_tool
    from agentflow.application.ports.summary_consumption import SummaryConsumptionPolicy
    from agentflow.application.tools.runtime import ToolDeniedError, ToolRuntime

    class Restricted(ToolRuntime):
        def prepare(self, *args, **kwargs):
            raise ToolDeniedError("deployment policy denies invocation")

    original = Restricted()
    scoped = original.with_tool(
        consumption_tool(SummaryConsumptionPolicy(mode="verified_tool"))
    )
    assert original.binding("read_verified_summary") is None
    with pytest.raises(ToolDeniedError):
        scoped.prepare(
            "read_verified_summary",
            {},
            allowed={"read_verified_summary"},
            side_effect=scoped.side_effect("read_verified_summary"),
        )
