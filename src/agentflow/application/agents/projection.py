"""Deterministic suffix selection AFTER complete AgentContextBuilder validation."""

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agentflow.application.agents.checkpoint_v2 import (
    EvidenceCandidates,
    carry_checkpoint,
    disclose_context,
    select_checkpoint,
)
from agentflow.application.agents.context import context_bytes
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionManifest,
    ContextProjectionPolicy,
)
from agentflow.application.ports.model_provider import AssistantMessage

if TYPE_CHECKING:
    from agentflow.application.agents.checkpoint import EvidenceCheckpoint


def digest_context(value) -> str:
    """SHA-256 of sorted-key, compact, finite JSON encoded as unescaped UTF-8."""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rounds(messages):
    """Return indivisible assistant/tool spans; system/user anchors stay in place."""
    rounds, seen, index = [], set(), 0
    while index < len(messages):
        message = messages[index]
        role = message["role"]
        if role in {"system", "user"} and isinstance(message.get("content"), str):
            index += 1
            continue
        if role != "assistant":
            raise ValueError
        assistant = AssistantMessage.model_validate(message)
        start = index
        index += 1
        for call in assistant.tool_calls:
            if call.id in seen:
                raise ValueError
            seen.add(call.id)
            observation = messages[index]
            if (
                observation.get("role") != "tool"
                or observation.get("tool_call_id") != call.id
                or not isinstance(observation.get("content"), str)
            ):
                raise ValueError
            index += 1
        rounds.append((start, index))
    return rounds


@dataclass(frozen=True)
class ProjectedContext:
    messages: list[dict]
    manifest: ContextProjectionManifest


def project_context(
    full: list[dict],
    tools: list[dict],
    policy: ContextProjectionPolicy,
    max_context_bytes: int,
    *,
    checkpoint: "EvidenceCheckpoint | EvidenceCandidates | None" = None,
) -> ProjectedContext:
    """Drop the fewest oldest complete rounds that make the request fit.

    The caller must first validate the entire persisted session with ContextBuilder.
    This additional protocol check also runs on the selected provider view.
    """
    try:
        if (policy.mode == "evidence_anchored_recent_rounds") != (
            checkpoint is not None
        ):
            raise ValueError
        rounds = _rounds(full)
        v2 = isinstance(checkpoint, EvidenceCandidates)
        if (
            checkpoint is not None
            and not v2
            and checkpoint.manifest.checkpoint_version != 1
        ):
            raise ValueError
        view = disclose_context(full, checkpoint) if v2 else full
        payload = {"messages": full, **({"tools": tools} if tools else {})}
        full_digest, schema_digest = digest_context(payload), digest_context(tools)
        removable = (
            max(
                0,
                len(rounds)
                - max(
                    policy.min_recent_rounds,
                    checkpoint.min_recent_rounds if checkpoint else 1,
                ),
            )
            if policy.mode != "full"
            else 0
        )
        last_failure = "AgentContextLimit"
        for omitted in range(removable + 1):
            removed = {i for start, end in rounds[:omitted] for i in range(start, end)}
            selected = [message for i, message in enumerate(view) if i not in removed]
            rendered = checkpoint
            if v2:
                retained = {
                    call["id"]
                    for message in selected
                    for call in message.get("tool_calls", [])
                }
                try:
                    rendered = select_checkpoint(checkpoint, retained)
                    selected = carry_checkpoint(selected, rendered)
                except ContextProjectionError as error:
                    last_failure = error.code
                    continue
            elif checkpoint is not None:
                # User-role data before the first retained assistant/tool span.
                insertion = next(
                    (i for i, m in enumerate(selected) if m["role"] == "assistant"),
                    len(selected),
                )
                selected.insert(insertion, checkpoint.message)
            size = context_bytes(selected, tools)
            if size > max_context_bytes:
                last_failure = "AgentContextLimit"
                continue
            _rounds(selected)
            return ProjectedContext(
                selected,
                ContextProjectionManifest(
                    projection_version=policy.projection_version,
                    selection_policy=policy.mode,
                    min_recent_rounds=max(
                        policy.min_recent_rounds,
                        checkpoint.min_recent_rounds if checkpoint else 1,
                    ),
                    max_context_bytes=max_context_bytes,
                    full_context_digest=full_digest,
                    projected_context_digest=digest_context(
                        {"messages": selected, **({"tools": tools} if tools else {})}
                    ),
                    projected_context_bytes=size,
                    included_round_count=len(rounds) - omitted,
                    omitted_round_count=omitted,
                    tool_schema_digest=schema_digest,
                    checkpoint=rendered.manifest if rendered else None,
                ),
            )
    except (
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise ContextProjectionError() from None
    raise ContextProjectionError(last_failure)
