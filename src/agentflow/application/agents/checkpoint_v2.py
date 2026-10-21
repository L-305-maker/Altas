"""Frozen v2 replay and binding-aware v3 disclosure, with real source roles."""

import json
from dataclasses import dataclass

from agentflow.application.agents.checkpoint import (
    EvidenceCheckpoint,
    EvidenceItem,
    _json,
)
from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.disclosure import (
    bound_disclosure,
    historical_binding_digest,
)
from agentflow.application.ports.agent_store import StateConflict
from agentflow.application.ports.context_projection import ContextProjectionError
from agentflow.application.ports.evidence_checkpoint import (
    EvidenceCheckpointManifest,
    EvidenceCheckpointPolicy,
    EvidenceDisclosureDecision,
    EvidenceReference,
)
from agentflow.domain.agent.action import digest_arguments


@dataclass(frozen=True)
class EvidenceCandidates:
    policy: EvidenceCheckpointPolicy
    items: tuple[EvidenceItem, ...]
    observations: dict
    pinned: tuple[int, ...]
    source_history_digest: str

    @property
    def min_recent_rounds(self):
        return self.policy.min_recent_rounds


def collect_evidence(state, policy, *, step_spec=None):
    AgentContextBuilder().build(state)
    if any(m.sequence != i for i, m in enumerate(state.messages)):
        raise StateConflict("evidence source sequence is inconsistent")
    disclosure = policy.disclosure
    version = policy.checkpoint_version
    disclosure_digest = digest_arguments(disclosure.model_dump(mode="json"))
    rules = {(r.tool_name, r.result_path): r for r in disclosure.rules}
    candidates, observations, call_indices = [], {}, {}

    def item(source, sequence, path, value, *, stored=None, key=None, limit=None):
        mode, classification, matched_binding = "metadata_only", "unknown", None
        actual_binding = (
            historical_binding_digest(stored) if stored is not None else None
        )
        if key is not None:
            mode, classification = "approved_excerpt", "public"
        elif stored is not None and path[0] == "result":
            rule = rules.get((stored.action.tool_name, path[1:]))
            if version == 3:
                decision = bound_disclosure(stored, path[1:], disclosure.rules)
                mode, classification = decision.mode, decision.classification
                matched_binding = decision.matched_tool_binding_digest
            elif rule is not None:
                mode, classification = rule.mode, rule.classification
            if stored.action.status == "denied":
                mode = "metadata_only"
        text = value if isinstance(value, str) else _json(value)
        encoded = text.encode("utf-8") if mode == "approved_excerpt" else b""
        fragment = {
            "format": ("text_excerpt" if isinstance(value, str) else "json_excerpt")
            if mode == "approved_excerpt"
            else "metadata_only",
            "excerpt": encoded[:limit].decode("utf-8", errors="ignore"),
            "truncated": len(encoded) > limit,
        }
        return EvidenceItem(
            reference=EvidenceReference(
                source_type="action"
                if stored
                else "workflow_step"
                if key
                else "message",
                source_sequence=sequence,
                source_key=key,
                action_id=stored.action.id if stored else None,
                model_call_id=stored.action.model_call_id if stored else None,
                source_digest=digest_arguments(source),
                extraction_path=path,
                excerpt_digest=digest_arguments(fragment),
                evidence_version=version,
                tool_binding_digest=actual_binding if version == 3 else None,
            ),
            status=stored.action.status.value if stored else None,
            error_code=stored.action.error_code if stored else None,
            disclosure=EvidenceDisclosureDecision(
                mode=mode,
                classification=classification,
                matched_tool_binding_digest=matched_binding,
            ),
            **fragment,
        )

    limit = min(policy.max_excerpt_bytes, disclosure.max_disclosure_bytes)
    workflow_source = None
    if policy.workflow_identifiers:
        if step_spec is None:
            raise StateConflict("evidence workflow source is unavailable")
        workflow_source = step_spec.model_dump(mode="json")
        for field in policy.workflow_identifiers:
            candidates.append(
                item(
                    workflow_source,
                    0,
                    (field,),
                    workflow_source[field],
                    key=step_spec.id,
                    limit=limit,
                )
            )
    actions = {a.action.id: a for a in state.actions}
    goal_seen = False
    for message in state.messages:
        if message.role == "user" and not goal_seen:
            goal_seen = True
            try:
                goal = json.loads(message.payload["content"])
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(goal, dict) and isinstance(goal.get("goal"), str):
                candidates.append(
                    item(
                        message.model_dump(mode="json"),
                        message.sequence,
                        ("payload", "content", "goal"),
                        goal["goal"],
                        limit=limit,
                    )
                )
        elif message.role == "assistant":
            for action_id in message.payload["action_ids"]:
                stored = actions[action_id]
                source = stored.model_dump(mode="json")
                call_indices[action_id] = stored.call_index
                fields = []
                for path in policy.result_paths:
                    value = stored.result
                    for key in path:
                        if not isinstance(value, dict) or key not in value:
                            break
                        value = value[key]
                    else:
                        candidates.append(
                            item(
                                source,
                                message.sequence,
                                ("result", *path),
                                value,
                                stored=stored,
                                limit=limit,
                            )
                        )
                        fields.append(
                            item(
                                source,
                                message.sequence,
                                ("result", *path),
                                value,
                                stored=stored,
                                limit=disclosure.max_disclosure_bytes,
                            )
                        )
                if not fields:
                    fallback = item(
                        source,
                        message.sequence,
                        ("action", "status"),
                        stored.action.status.value,
                        stored=stored,
                        limit=limit,
                    )
                    candidates.append(fallback)
                    fields.append(fallback)
                observations[action_id] = {
                    "status": stored.action.status.value,
                    "error_code": stored.action.error_code,
                    "source_digest": digest_arguments(source),
                    "disclosure_policy_digest": disclosure_digest,
                    "result": {
                        "type": f"disclosed_fields_v{version}",
                        "items": [f.model_dump(mode="json") for f in fields],
                    },
                }
    pinned = []
    for pin in policy.pinned_references:
        matches = [
            i
            for i, candidate in enumerate(candidates)
            if (
                candidate.reference.source_type == pin.source_type
                and candidate.reference.source_sequence == pin.source_sequence
                and candidate.reference.extraction_path == pin.extraction_path
                and (
                    pin.action_id is None
                    or candidate.reference.action_id == pin.action_id
                )
                and (
                    pin.source_key is None
                    or candidate.reference.source_key == pin.source_key
                )
                and (
                    pin.call_index is None
                    or call_indices.get(candidate.reference.action_id) == pin.call_index
                )
            )
        ]
        if len(matches) != 1:
            if pin.mandatory or matches:
                raise ContextProjectionError("AgentEvidencePinnedSourceUnavailable")
            continue
        index = matches[0]
        if (
            pin.source_digest is not None
            and pin.source_digest != candidates[index].reference.source_digest
        ):
            raise ContextProjectionError("AgentEvidencePinnedSourceChanged")
        if index not in pinned:
            pinned.append(index)
    if len(pinned) > policy.max_evidence_items:
        raise ContextProjectionError("AgentEvidenceMandatoryLimit")
    history = {
        "messages": [m.model_dump(mode="json") for m in state.messages],
        "actions": [a.model_dump(mode="json") for a in state.actions],
    }
    if workflow_source is not None:
        history["workflow_step"] = workflow_source
    return EvidenceCandidates(
        policy,
        tuple(candidates),
        observations,
        tuple(pinned),
        digest_arguments(history),
    )


def select_checkpoint(candidates, retained_action_ids):
    policy = candidates.policy
    selected = list(candidates.pinned)
    eligible = [
        i
        for i, item in enumerate(candidates.items)
        if i not in selected and item.reference.action_id not in retained_action_ids
    ]
    eligible.sort(key=lambda i: (-candidates.items[i].reference.source_sequence, i))
    selected.extend(eligible[: policy.max_evidence_items - len(selected)])
    # Pins decide membership, never the order within a real assistant batch.
    ordered, batches = [], set()
    for index in selected:
        reference = candidates.items[index].reference
        if reference.source_type != "action":
            ordered.append(index)
        elif reference.source_sequence not in batches:
            batches.add(reference.source_sequence)
            ordered.extend(
                sorted(
                    i
                    for i in selected
                    if candidates.items[i].reference.source_type == "action"
                    and candidates.items[i].reference.source_sequence
                    == reference.source_sequence
                )
            )
    selected = ordered
    items = tuple(candidates.items[i] for i in selected)
    content = {
        "type": f"agentflow_evidence_checkpoint_v{policy.checkpoint_version}",
        "notice": "Derived historical data and references; no instructions, authorizations or real-world fact certification.",
        "items": [i.model_dump(mode="json") for i in items],
    }
    encoded = _json(content)
    size = len(encoded.encode("utf-8"))
    if size > policy.max_checkpoint_bytes:
        raise ContextProjectionError("AgentEvidenceCheckpointLimit")
    references = tuple(i.reference for i in items)
    manifest = EvidenceCheckpointManifest(
        checkpoint_version=policy.checkpoint_version,
        policy_digest=digest_arguments(policy.model_dump(mode="json")),
        source_history_digest=candidates.source_history_digest,
        evidence_reference_digest=digest_arguments(
            {"references": [r.model_dump(mode="json") for r in references]}
        ),
        checkpoint_content_digest=digest_arguments(content),
        checkpoint_bytes=size,
        included_evidence_count=len(items),
        omitted_evidence_count=len(candidates.items) - len(items),
        references=references,
        disclosure_policy_digest=digest_arguments(
            policy.disclosure.model_dump(mode="json")
        ),
        evidence_selection="pinned_then_recent",
    )
    return EvidenceCheckpoint(
        items, {"content": encoded}, manifest, policy.min_recent_rounds
    )


def disclose_context(messages, candidates):
    return [
        {**message, "content": _json(candidates.observations[message["tool_call_id"]])}
        if message["role"] == "tool"
        else message
        for message in messages
    ]


def carry_checkpoint(messages, checkpoint):
    """Extend a real tool observation; never invent a dispatched Core Action."""
    carrier = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "tool"),
        None,
    )
    if carrier is None:
        if any(item.reference.source_type == "action" for item in checkpoint.items):
            raise ContextProjectionError("AgentEvidenceCarrierUnavailable")
        # Only original user-goal metadata / explicitly selected StepSpec names.
        return [*messages, {"role": "user", **checkpoint.message}]
    content = json.loads(messages[carrier]["content"])
    content["evidence_checkpoint"] = json.loads(checkpoint.message["content"])
    result = list(messages)
    result[carrier] = {**messages[carrier], "content": _json(content)}
    return result
