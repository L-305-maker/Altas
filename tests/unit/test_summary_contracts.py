import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_agent_context import session
from test_disclosure_binding import PRIVATE, PUBLIC, binding, bound_history, v3_policy

from agentflow.application.agents.summary import (
    SummarySourceError,
    build_summary_input,
    summary_messages,
)
from agentflow.application.ports.durable_summary import (
    DurableSummaryPolicy,
    SummaryDraft,
    SummaryInput,
)


def source_input(**changes):
    source = bound_history(binding(version="v2"), rounds=1)
    data = source.model_dump()
    data["snapshot"]["status"] = "succeeded"
    data["snapshot"]["actions"] = ()
    source = type(source).model_validate(data)
    evidence = v3_policy()
    policy = DurableSummaryPolicy(
        enabled=True,
        source_evidence=evidence,
        source_egress={
            "disclosure_mode": "bound_disclosure",
            "rules": evidence.disclosure.rules,
        },
        **changes,
    )
    plan = build_summary_input(
        source,
        policy,
        run_id="run",
        source_step=SimpleNamespace(id="source-row", key="source"),
        source_updated_at=123.0,
        owner_step_id="summary-row",
    )
    return source, plan


def test_summary_input_is_deterministic_frozen_and_contains_only_authorized_evidence():
    source, first = source_input()
    _, second = source_input()
    assert (
        first == second
        and first.manifest.summary_input_digest == second.manifest.summary_input_digest
    )
    raw = json.dumps(first.tool_result())
    assert PRIVATE not in raw and "SYNTHETIC_NESTED_PRIVATE" not in raw
    assert PUBLIC in raw
    assert all(e.reference.source_type == "action" for e in first.evidence)
    assert all(
        e.reference.extraction_path == ("action", "status")
        for e in first.evidence
        if e.disclosure.mode == "metadata_only"
    )
    assert PRIVATE in json.dumps([a.result for a in source.actions])
    assert PRIVATE not in json.dumps(
        [m.payload["content"] for m in summary_messages(first)]
    )
    with pytest.raises(ValidationError):
        first.source_snapshot.source_revision = 999
    changed = first.model_dump()
    changed["evidence"][0]["excerpt"] = "forged"
    with pytest.raises(ValidationError):
        SummaryInput.model_validate(changed)


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "true"},
        {"summary_version": True},
        {"max_summary_bytes": 0},
        {"max_summary_model_attempts": 1},
        {"max_summary_model_rounds": 9},
        {"required_evidence_coverage": float("nan")},
        {"allow_uncertain_claims": 1},
        {"source_session_id": "foreign"},
        {"max_source_bytes": 100_000},
    ],
)
def test_summary_policy_rejects_unsafe_or_unbounded_configuration(changes):
    with pytest.raises(ValidationError):
        DurableSummaryPolicy.model_validate(changes)


def test_unknown_source_cannot_be_hidden_by_evidence_selection():
    _, plan = source_input()
    with pytest.raises(SummarySourceError, match="SummarySourceNotTerminal"):
        build_summary_input(
            session(("unknown",), rounds=2),
            plan.policy,
            run_id="run",
            source_step=SimpleNamespace(id="step", key="source"),
            source_updated_at=123.0,
            owner_step_id="summary",
        )


def test_model_confidence_probability_and_unattributed_fact_are_not_accepted():
    claim = {
        "claim_id": "claim",
        "text": "claim",
        "evidence_references": [],
        "uncertainty": "evidence_linked",
        "source_attribution_status": "unattributed",
    }
    with pytest.raises(ValidationError):
        SummaryDraft(claims=[claim])
    with pytest.raises(ValidationError):
        SummaryDraft(claims=[{**claim, "uncertainty": "unknown", "confidence": 0.99}])
