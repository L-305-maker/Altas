"""Nonprivileged evidence identities; never contain source content or authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from agentflow.domain.agent.action import Identifier, digest_arguments

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PathPart = Annotated[str, Field(min_length=1, max_length=80)]
Path = Annotated[tuple[PathPart, ...], Field(max_length=8)]
Version = Annotated[int, Field(ge=1, le=1, strict=True)]


class EvidenceCheckpointPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["disabled", "anchored_extracts"] = "disabled"
    checkpoint_version: Version = 1
    max_evidence_items: Annotated[int, Field(ge=1, le=32, strict=True)] = 8
    max_checkpoint_bytes: Annotated[int, Field(ge=1, le=64_000, strict=True)] = 16_000
    max_excerpt_bytes: Annotated[int, Field(ge=1, le=4096, strict=True)] = 256
    min_recent_rounds: Annotated[int, Field(ge=1, strict=True)] = 1
    # Object-key paths only, in trusted policy order; absent paths are not facts.
    result_paths: Annotated[
        tuple[Annotated[tuple[PathPart, ...], Field(max_length=7)], ...],
        Field(max_length=8),
    ] = (("value",),)
    workflow_identifiers: Annotated[
        tuple[Literal["id", "handler"], ...], Field(max_length=2)
    ] = ()

    @model_serializer(mode="wrap")
    def legacy_policy(self, handler):
        data = handler(self)
        if not self.workflow_identifiers:
            data.pop("workflow_identifiers", None)
        return data


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_type: Literal["message", "action", "workflow_step"]
    source_sequence: Annotated[int, Field(ge=0, strict=True)]
    action_id: Identifier | None = None
    model_call_id: Identifier | None = None
    source_digest: Digest
    extraction_path: Path
    excerpt_digest: Digest
    evidence_version: Version = 1
    source_key: Identifier | None = None

    @model_serializer(mode="wrap")
    def legacy_reference(self, handler):
        data = handler(self)
        if self.source_key is None:
            data.pop("source_key", None)
        return data

    @model_validator(mode="after")
    def source_identity(self):
        if (
            (
                self.source_type == "action"
                and (self.action_id is None or self.model_call_id is None)
            )
            or (
                self.source_type != "action"
                and (self.action_id is not None or self.model_call_id is not None)
            )
            or ((self.source_type == "workflow_step") != (self.source_key is not None))
            or (self.source_type == "workflow_step" and self.source_sequence != 0)
        ):
            raise ValueError("evidence source identity differs")
        return self


class EvidenceCheckpointManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checkpoint_version: Version = 1
    policy_digest: Digest
    source_history_digest: Digest
    evidence_reference_digest: Digest
    checkpoint_content_digest: Digest
    checkpoint_bytes: Annotated[int, Field(ge=1, le=64_000, strict=True)]
    included_evidence_count: Annotated[int, Field(ge=0, le=32, strict=True)]
    omitted_evidence_count: Annotated[int, Field(ge=0, strict=True)]
    references: Annotated[tuple[EvidenceReference, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def reference_identity(self):
        if self.included_evidence_count != len(self.references) or (
            digest_arguments(
                {"references": [r.model_dump(mode="json") for r in self.references]}
            )
            != self.evidence_reference_digest
        ):
            raise ValueError("evidence references differ")
        return self
