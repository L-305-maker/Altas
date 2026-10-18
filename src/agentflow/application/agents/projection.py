"""Deterministic suffix selection AFTER complete AgentContextBuilder validation."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from agentflow.application.agents.context import context_bytes
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionManifest,
    ContextProjectionPolicy,
)
from agentflow.application.ports.model_provider import AssistantMessage


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
) -> ProjectedContext:
    """Drop the fewest oldest complete rounds that make the request fit.

    The caller must first validate the entire persisted session with ContextBuilder.
    This additional protocol check also runs on the selected provider view.
    """
    try:
        rounds = _rounds(full)
        payload = {"messages": full, **({"tools": tools} if tools else {})}
        full_digest, schema_digest = digest_context(payload), digest_context(tools)
        removable = (
            max(0, len(rounds) - policy.min_recent_rounds)
            if policy.mode == "recent_complete_rounds"
            else 0
        )
        for omitted in range(removable + 1):
            removed = {i for start, end in rounds[:omitted] for i in range(start, end)}
            selected = [message for i, message in enumerate(full) if i not in removed]
            size = context_bytes(selected, tools)
            if size > max_context_bytes:
                continue
            _rounds(selected)
            return ProjectedContext(
                selected,
                ContextProjectionManifest(
                    projection_version=policy.projection_version,
                    selection_policy=policy.mode,
                    min_recent_rounds=policy.min_recent_rounds,
                    max_context_bytes=max_context_bytes,
                    full_context_digest=full_digest,
                    projected_context_digest=digest_context(
                        {"messages": selected, **({"tools": tools} if tools else {})}
                    ),
                    projected_context_bytes=size,
                    included_round_count=len(rounds) - omitted,
                    omitted_round_count=omitted,
                    tool_schema_digest=schema_digest,
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
    raise ContextProjectionError("AgentContextLimit")
