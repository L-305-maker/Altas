"""Synthetic counters test Core binding only; never a real provider guarantee."""

import hashlib

from agentflow.application.ports.model_request import ProviderCapabilities, TokenCount

ASSET = b"synthetic-public-test-asset-v1"
DIGEST = hashlib.sha256(ASSET).hexdigest()


def capabilities(**changes):
    return ProviderCapabilities(
        **{
            "provider_key": "mock",
            "model": "mock-v1",
            "capability_version": "1",
            "supports_tools": True,
            "supports_json_output": True,
            "max_output_tokens": 100,
            "context_window_tokens": 100,
            "token_counting_method": "fixture-v1",
            "token_counting_assurance": "exact",
            **changes,
        }
    )


def profile(**changes):
    from agentflow.application.ports.model_request import TokenCounterProfile

    return TokenCounterProfile(
        **{
            "provider_key": "mock",
            "model": "mock-v1",
            "adapter_version": "mock.v1",
            "capability_version": "1",
            "token_counting_method": "fixture-v1",
            "counting_assurance": "exact",
            "tokenizer_asset_digest": DIGEST,
            "chat_template_version": "1",
            "chat_template_digest": DIGEST,
            "framing_version": "1",
            "framing_digest": DIGEST,
            "evidence_digest": DIGEST,
            "context_window_tokens": 100,
            "max_output_tokens": 100,
            "profile_version": "1",
            **changes,
        }
    )


class Counter:
    def __init__(self, *, profile=None, tokens=90):
        self.profile = profile or globals()["profile"]()
        self.tokens = tokens
        self.payloads = []

    def count(self, payload):
        self.payloads.append(payload)
        return TokenCount(
            input_tokens=self.tokens,
            assurance=self.profile.counting_assurance,
            method=self.profile.token_counting_method,
        )
