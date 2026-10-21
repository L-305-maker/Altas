"""Provider-only view. Never modifies audit records or consults current tools."""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentflow.application.agents.disclosure import (
    bound_disclosure,
    historical_binding_digest,
)
from agentflow.application.ports.agent_store import AgentSession
from agentflow.application.ports.context_egress import (
    ProviderContextEgressManifest,
    ProviderContextEgressPolicy,
)
from agentflow.application.ports.context_projection import ContextProjectionError
from agentflow.domain.agent.action import digest_arguments

if TYPE_CHECKING:
    from agentflow.application.agents.checkpoint import EvidenceCheckpoint
    from agentflow.application.agents.checkpoint_v2 import EvidenceCandidates


def encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class EgressContext:
    messages: list[dict] = field(repr=False)
    manifest: ProviderContextEgressManifest


def prepare_egress(
    full: list[dict],
    state: AgentSession,
    policy: ProviderContextEgressPolicy | None,
    checkpoint: "EvidenceCheckpoint | EvidenceCandidates | None" = None,
) -> EgressContext | None:
    """Called only after complete history validation, before window selection.

    Checkpoint's frozen calculation is not rewritten. Conflicting policies are
    rejected, including metadata-only egress combined with public checkpoint rules.
    """
    if policy is None:
        return None
    if checkpoint is not None:
        from agentflow.application.agents.checkpoint_v2 import (
            EvidenceCandidates,
            disclose_context,
        )

        if (
            not isinstance(checkpoint, EvidenceCandidates)
            or checkpoint.policy.checkpoint_version != 3
            or checkpoint.policy.disclosure != policy.disclosure_policy()
        ):
            raise ContextProjectionError("AgentContextEgressPolicyConflict")
        # v3 is the sole renderer for this explicitly identical policy.
        view = disclose_context(full, checkpoint)
    else:
        observations = {}
        for stored in state.actions:
            fields = []
            for rule in policy.rules:
                if rule.binding != stored.tool_binding:
                    continue
                decision = bound_disclosure(stored, rule.result_path, policy.rules)
                if decision.mode != "approved_excerpt":
                    continue
                value = stored.result
                for part in rule.result_path:
                    if not isinstance(value, dict) or part not in value:
                        break
                    value = value[part]
                else:
                    if (
                        len(encoded(value).encode("utf-8"))
                        > policy.max_disclosure_bytes
                    ):
                        raise ContextProjectionError("AgentToolDisclosureLimit")
                    fields.append(
                        {
                            "path": rule.result_path,
                            "disclosure": decision.model_dump(mode="json"),
                            "value": value,
                        }
                    )
            observations[stored.action.id] = {
                "status": stored.action.status.value,
                "error_code": stored.action.error_code,
                "result": {"type": "provider_tool_disclosure_v1", "fields": fields},
                "tool_binding_digest": historical_binding_digest(stored),
            }
        view = [
            {**m, "content": encoded(observations[m["tool_call_id"]])}
            if m["role"] == "tool"
            else m
            for m in full
        ]
    return EgressContext(
        view,
        ProviderContextEgressManifest(
            policy_digest=policy.digest,
            source_actions_digest=digest_arguments(
                [a.model_dump(mode="json") for a in state.actions]
            ),
            observations_digest=digest_arguments(
                [m for m in view if m["role"] == "tool"]
            ),
        ),
    )
