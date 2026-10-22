"""Offline startup checks for deployment-owned counters. No network/asset loader.

This detects regression and asset drift; it cannot prove a hosted provider uses
these assets. Review that evidence independently before installing a profile.
"""

import hashlib
import json
from dataclasses import dataclass, field

from agentflow.application.ports.model_request import (
    ModelRequestError,
    TokenCount,
    TokenCounter,
    TokenCounterProfile,
)


@dataclass(frozen=True)
class TokenCounterVector:
    request_json: str = field(repr=False)
    input_tokens: int


def verify_counter_conformance(
    counter: TokenCounter,
    *,
    tokenizer_asset: bytes,
    chat_template_asset: bytes,
    framing_asset: bytes,
    evidence: bytes,
    vectors: tuple[TokenCounterVector, ...],
):
    """Check pinned assets and independently authored, public full-wire vectors.

    For upper bounds the expected count must be an independently known actual
    input count. Never manufacture that oracle from the counter under test.
    """
    try:
        profile = TokenCounterProfile.model_validate(counter.profile.model_dump())
        for asset, digest in (
            (tokenizer_asset, profile.tokenizer_asset_digest),
            (chat_template_asset, profile.chat_template_digest),
            (framing_asset, profile.framing_digest),
            (evidence, profile.evidence_digest),
        ):
            if not isinstance(asset, bytes) or not 0 < len(asset) <= 32_000_000:
                raise ValueError
            if hashlib.sha256(asset).hexdigest() != digest:
                raise ValueError
        if not 1 <= len(vectors) <= 128:
            raise ValueError
        for vector in vectors:
            if (
                type(vector.input_tokens) is not int
                or vector.input_tokens < 0
                or len(vector.request_json.encode("utf-8")) > 100_000
                or json.loads(vector.request_json)["model"] != profile.model
            ):
                raise ValueError
            count = counter.count(vector.request_json)
            if not isinstance(count, TokenCount):
                raise ValueError
            count = TokenCount.model_validate(count.model_dump())
            if (
                count.assurance != profile.counting_assurance
                or count.method != profile.token_counting_method
                or count.input_tokens < vector.input_tokens
                or (
                    count.assurance == "exact"
                    and count.input_tokens != vector.input_tokens
                )
                or counter.profile != profile
            ):
                raise ValueError
    except Exception:
        raise ModelRequestError("ModelTokenCounterInvalid") from None
