"""Frozen summary provenance and bounded generated output; no model/DB calls."""

import json
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.application.ports.context_egress import ProviderContextEgressPolicy
from agentflow.application.ports.evidence_checkpoint import (
    Digest,
    EvidenceCheckpointPolicy,
    EvidenceDisclosureDecision,
    EvidenceReference,
)
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.domain.agent.action import ErrorCode, Identifier, digest_arguments
from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import Name

HANDLER = "durable_summary"
TOOL_NAME = "read_summary_evidence"
Version = Annotated[int, Field(ge=1, le=1, strict=True)]
Clock = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def metadata_evidence_policy():
    return EvidenceCheckpointPolicy(
        mode="anchored_extracts",
        checkpoint_version=3,
        selection_policy="pinned_then_recent",
        disclosure={"policy_version": 2},
    )


class DurableSummaryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = Field(default=False, strict=True)
    summary_version: Version = 1
    summary_output_schema_version: Version = 1
    source_step: Name = "source"
    max_summary_bytes: Annotated[int, Field(ge=256, le=32_000, strict=True)] = 16_000
    max_summary_model_rounds: Annotated[int, Field(ge=2, le=4, strict=True)] = 2
    max_summary_model_attempts: Annotated[int, Field(ge=2, le=8, strict=True)] = 3
    max_source_bytes: Annotated[int, Field(ge=256, le=4096, strict=True)] = 4096
    max_context_bytes: Annotated[int, Field(ge=1024, le=100_000, strict=True)] = 32_000
    required_evidence_coverage: Annotated[
        float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    ] = 1.0
    allow_uncertain_claims: bool = Field(default=False, strict=True)
    source_egress: ProviderContextEgressPolicy = Field(
        default_factory=ProviderContextEgressPolicy
    )
    source_evidence: EvidenceCheckpointPolicy = Field(
        default_factory=metadata_evidence_policy
    )
    observed_usage: ObservedUsagePolicy | None = None
    retry_base_seconds: Annotated[
        float, Field(gt=0, le=3600, strict=True, allow_inf_nan=False)
    ] = 1.0
    retry_max_seconds: Annotated[
        float, Field(gt=0, le=3600, strict=True, allow_inf_nan=False)
    ] = 60.0

    @model_validator(mode="after")
    def safe_policy(self):
        evidence = self.source_evidence
        if (
            evidence.mode != "anchored_extracts"
            or evidence.checkpoint_version != 3
            or evidence.disclosure != self.source_egress.disclosure_policy()
            or evidence.workflow_identifiers
            or evidence.pinned_references
        ):
            raise ValueError(
                "summary v1 requires identical bound disclosure and action-only sources"
            )
        if (
            self.retry_base_seconds > self.retry_max_seconds
            or self.max_summary_model_attempts < self.max_summary_model_rounds
        ):
            raise ValueError("summary retry and budget limits are inconsistent")
        return self

    @property
    def source_disclosure_policy_digest(self):
        return digest_arguments(
            {
                "egress": self.source_egress.model_dump(mode="json"),
                "evidence": self.source_evidence.disclosure.model_dump(mode="json"),
            }
        )


class SummarySourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_run_id: Identifier
    source_step_id: Identifier
    source_step_key: Name
    source_session_id: Identifier
    source_revision: Annotated[int, Field(ge=0, strict=True)]
    source_history_digest: Digest
    evidence_disclosure_identity: Digest
    evidence_reference_digest: Digest
    allowed_source_types: Annotated[
        tuple[Literal["action"], ...], Field(min_length=1, max_length=1)
    ] = ("action",)
    created_at: Clock
    terminal_state: Literal["succeeded"] = "succeeded"
    completeness: Literal["selected_evidence_only"] = "selected_evidence_only"
    omitted_evidence_count: Annotated[int, Field(ge=0, strict=True)]


class SummaryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference_id: Digest
    reference: EvidenceReference
    status: Literal["succeeded", "failed", "denied"]
    error_code: ErrorCode | None = None
    disclosure: EvidenceDisclosureDecision
    format: Literal["metadata_only", "text_excerpt", "json_excerpt"]
    excerpt: Annotated[str, Field(max_length=4096, strict=True)]
    truncated: bool = Field(strict=True)

    @model_validator(mode="after")
    def authorized_reference(self):
        if (
            self.reference_id
            != digest_arguments(self.reference.model_dump(mode="json"))
            or self.reference.source_type != "action"
            or self.reference.evidence_version != 3
        ):
            raise ValueError("summary evidence reference differs")
        if self.disclosure.mode == "metadata_only":
            if (
                self.excerpt
                or self.reference.extraction_path != ("action", "status")
                or self.format != "metadata_only"
            ):
                raise ValueError(
                    "summary metadata cannot reference private result fields"
                )
        elif (
            self.status == "denied"
            or self.disclosure.classification != "public"
            or self.disclosure.matched_tool_binding_digest is None
            or self.disclosure.matched_tool_binding_digest
            != self.reference.tool_binding_digest
            or not self.reference.extraction_path
            or self.reference.extraction_path[0] != "result"
            or self.format == "metadata_only"
        ):
            raise ValueError("summary excerpt requires a real public binding")
        return self


class SummaryInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary_input_version: Version = 1
    owner_step_id: Identifier
    request_id: Digest
    policy_digest: Digest
    source_snapshot_digest: Digest
    evidence_reference_digest: Digest
    summary_input_digest: Digest
    input_bytes: Annotated[int, Field(ge=1, le=4096, strict=True)]


def summary_manifest(policy, source, evidence, owner_step_id):
    policy_digest = digest_arguments(policy.model_dump(mode="json"))
    snapshot_digest = digest_arguments(source.model_dump(mode="json"))
    request_id = digest_arguments(
        {"source": snapshot_digest, "policy": policy_digest, "owner": owner_step_id}
    )
    input_digest = digest_arguments(
        {
            "request_id": request_id,
            "evidence": [e.model_dump(mode="json") for e in evidence],
        }
    )
    result = {
        "type": "summary_evidence_v1",
        "input_digest": input_digest,
        "evidence": [e.model_dump(mode="json") for e in evidence],
    }
    if len(canonical(result).encode("utf-8")) > policy.max_source_bytes:
        raise ValueError("SummaryInputLimit")
    return SummaryInputManifest(
        owner_step_id=owner_step_id,
        request_id=request_id,
        policy_digest=policy_digest,
        source_snapshot_digest=snapshot_digest,
        evidence_reference_digest=source.evidence_reference_digest,
        summary_input_digest=input_digest,
        input_bytes=len(canonical(result).encode("utf-8")),
    )


class SummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: DurableSummaryPolicy
    source_snapshot: SummarySourceSnapshot
    evidence: Annotated[tuple[SummaryEvidence, ...], Field(min_length=1, max_length=32)]
    manifest: SummaryInputManifest

    @model_validator(mode="after")
    def valid_input(self):
        refs = [e.reference.model_dump(mode="json") for e in self.evidence]
        if (
            len({e.reference_id for e in self.evidence}) != len(self.evidence)
            or self.source_snapshot.evidence_reference_digest
            != digest_arguments({"references": refs})
            or self.source_snapshot.evidence_disclosure_identity
            != self.policy.source_disclosure_policy_digest
            or self.manifest
            != summary_manifest(
                self.policy,
                self.source_snapshot,
                self.evidence,
                self.manifest.owner_step_id,
            )
        ):
            raise ValueError("summary input identity differs")
        return self

    def tool_result(self):
        return {
            "type": "summary_evidence_v1",
            "input_digest": self.manifest.summary_input_digest,
            "evidence": [e.model_dump(mode="json") for e in self.evidence],
        }


class SummaryClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: Name
    text: Annotated[str, Field(min_length=1, max_length=2000, strict=True)]
    evidence_references: Annotated[tuple[Digest, ...], Field(max_length=32)]
    uncertainty: Literal["evidence_linked", "inference", "unknown"]
    source_attribution_status: Literal["referenced", "unattributed"]

    @model_validator(mode="after")
    def explicit_attribution(self):
        if not self.text.strip() or len(set(self.evidence_references)) != len(
            self.evidence_references
        ):
            raise ValueError("summary claim is empty or repeats references")
        if bool(self.evidence_references) != (
            self.source_attribution_status == "referenced"
        ) or (self.uncertainty == "evidence_linked" and not self.evidence_references):
            raise ValueError("summary attribution is inconsistent")
        return self


class SummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary_output_schema_version: Version = 1
    claims: Annotated[tuple[SummaryClaim, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def unique_claims(self):
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("summary claim identities must be unique")
        return self


class SummaryAttemptReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: Identifier
    model_call_id: Identifier
    attempt_index: Annotated[int, Field(ge=1, strict=True)]


class SummaryArtifact(SummaryDraft):
    summary_version: Version = 1
    request_id: Digest
    source_snapshot: SummarySourceSnapshot
    source_snapshot_digest: Digest
    policy_digest: Digest
    summary_input_digest: Digest
    summary_request_contract_digest: Digest
    summary_attempt: SummaryAttemptReference
    summary_content_digest: Digest
    generated_at: Clock
    evidence_coverage: Annotated[
        float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    ]
    omitted_evidence_references: Annotated[tuple[Digest, ...], Field(max_length=32)]
    assurance: Literal["generated_not_fact_verified"] = "generated_not_fact_verified"

    @model_validator(mode="after")
    def content_identity(self):
        if self.source_snapshot_digest != digest_arguments(
            self.source_snapshot.model_dump(mode="json")
        ) or self.summary_content_digest != digest_arguments(
            self.model_dump(
                mode="json",
                include={"claims", "evidence_coverage", "omitted_evidence_references"},
            )
        ):
            raise ValueError("summary artifact digest differs")
        return self


class SummarySourceStore(Protocol):
    def summary_source(self, task: Task) -> SummaryInput:
        """Owned, same-run stable dependency only; never accepts arbitrary session IDs."""
        ...
