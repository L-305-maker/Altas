"""Opt-in tool observation egress; absence preserves historical request identity."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.application.ports.evidence_checkpoint import (
    Digest,
    EvidenceDisclosurePolicy,
    EvidenceDisclosureRule,
)
from agentflow.domain.agent.action import digest_arguments


class ProviderContextEgressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_version: Annotated[int, Field(ge=1, le=1, strict=True)] = 1
    disclosure_mode: Literal["metadata_only", "bound_disclosure"] = "metadata_only"
    max_disclosure_bytes: Annotated[int, Field(ge=1, le=4096, strict=True)] = 256
    rules: Annotated[tuple[EvidenceDisclosureRule, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def bound_rules(self):
        if self.disclosure_mode == "metadata_only" and self.rules:
            raise ValueError("metadata-only policy cannot grant disclosure")
        self.disclosure_policy()  # Reuse v3's full binding and uniqueness contract.
        return self

    def disclosure_policy(self) -> EvidenceDisclosurePolicy:
        return EvidenceDisclosurePolicy(
            policy_version=2,
            max_disclosure_bytes=self.max_disclosure_bytes,
            rules=self.rules,
        )

    @property
    def digest(self) -> str:
        return digest_arguments(self.model_dump(mode="json"))


class ProviderContextEgressManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    egress_version: Annotated[int, Field(ge=1, le=1, strict=True)] = 1
    policy_digest: Digest
    source_actions_digest: Digest
    observations_digest: Digest
