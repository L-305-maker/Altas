"""One binding decision for checkpoint v3 and provider observation egress."""

from agentflow.application.ports.agent_store import StoredAction
from agentflow.application.ports.evidence_checkpoint import (
    EvidenceDisclosureDecision,
    EvidenceDisclosureRule,
)
from agentflow.domain.agent.action import digest_arguments


def historical_binding_digest(stored: StoredAction) -> str | None:
    binding = stored.tool_binding
    return (
        digest_arguments(binding.model_dump(mode="json"))
        if binding is not None and binding.matches(stored.action)
        else None
    )


def bound_disclosure(
    stored: StoredAction,
    path: tuple[str, ...],
    rules: tuple[EvidenceDisclosureRule, ...],
) -> EvidenceDisclosureDecision:
    actual = historical_binding_digest(stored)
    rule = next(
        (
            rule
            for rule in rules
            if actual is not None
            and rule.binding == stored.tool_binding
            and rule.tool_name == stored.action.tool_name
            and rule.result_path == path
        ),
        None,
    )
    return EvidenceDisclosureDecision(
        mode=rule.mode
        if rule and stored.action.status != "denied"
        else "metadata_only",
        classification=rule.classification if rule else "unknown",
        matched_tool_binding_digest=actual if rule else None,
    )
