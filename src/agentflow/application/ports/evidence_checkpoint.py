"""Nonprivileged evidence identities; never contain source content or authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from agentflow.domain.agent.action import Identifier, digest_arguments

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PathPart = Annotated[str, Field(min_length=1, max_length=80)]
Path = Annotated[tuple[PathPart, ...], Field(max_length=8)]
Version = Annotated[int, Field(ge=1, le=2, strict=True)]
Classification = Literal["public", "sensitive", "unknown"]
DisclosureMode = Literal["metadata_only", "approved_excerpt"]


class EvidenceDisclosureRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_name: Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,79}$")]
    result_path: Annotated[tuple[PathPart, ...], Field(max_length=7)]
    mode: DisclosureMode = "metadata_only"
    classification: Classification = "unknown"

    @model_validator(mode="after")
    def explicit_public_only(self):
        if self.mode == "approved_excerpt" and self.classification != "public":
            raise ValueError("only explicitly public fields permit excerpts")
        return self


class EvidenceDisclosurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_type: Literal["action"] = "action"
    policy_version: Annotated[int, Field(ge=1, le=1, strict=True)] = 1
    # Per-field raw UTF-8 limit; the whole request still has max_context_bytes.
    max_disclosure_bytes: Annotated[int, Field(ge=1, le=4096, strict=True)] = 256
    rules: Annotated[tuple[EvidenceDisclosureRule, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def unambiguous_rules(self):
        identities = [(r.tool_name, r.result_path) for r in self.rules]
        if len(set(identities)) != len(identities):
            raise ValueError("disclosure paths must be unique per tool")
        return self


class EvidenceDisclosureDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: DisclosureMode
    classification: Classification


class PinnedEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_type: Literal["message", "action", "workflow_step"]
    source_sequence: Annotated[int, Field(ge=0, strict=True)]
    extraction_path: Path
    action_id: Identifier | None = None
    call_index: Annotated[int, Field(ge=0, strict=True)] | None = None
    source_key: Identifier | None = None
    source_digest: Digest | None = None
    mandatory: bool = True

    @model_validator(mode="after")
    def locatable_action(self):
        if (
            self.source_type == "action"
            and self.action_id is None
            and self.call_index is None
        ):
            raise ValueError("action pins require a persisted action identity or index")
        return self


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
    selection_policy: Literal["legacy_earliest", "pinned_then_recent"] | None = None
    disclosure: EvidenceDisclosurePolicy | None = None
    pinned_references: Annotated[
        tuple[PinnedEvidenceReference, ...], Field(max_length=32)
    ] = ()

    @model_validator(mode="after")
    def versioned_policy(self):
        if self.checkpoint_version == 1:
            if (
                self.disclosure is not None
                or self.pinned_references
                or self.selection_policy not in {None, "legacy_earliest"}
            ):
                raise ValueError(
                    "historical v1 cannot acquire new disclosure semantics"
                )
        elif self.disclosure is None or self.selection_policy != "pinned_then_recent":
            raise ValueError("v2 requires explicit disclosure and selection policies")
        elif len(set(self.result_paths)) != len(self.result_paths) or len(
            set(self.workflow_identifiers)
        ) != len(self.workflow_identifiers):
            raise ValueError("v2 evidence sources must be unique")
        return self

    @model_serializer(mode="wrap")
    def legacy_policy(self, handler):
        data = handler(self)
        if not self.workflow_identifiers:
            data.pop("workflow_identifiers", None)
        for field in ("selection_policy", "disclosure", "pinned_references"):
            if not getattr(self, field):
                data.pop(field, None)
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
    disclosure_policy_digest: Digest | None = None
    evidence_selection: Literal["pinned_then_recent"] | None = None

    @model_serializer(mode="wrap")
    def historical_manifest(self, handler):
        data = handler(self)
        if self.checkpoint_version == 1:
            data.pop("disclosure_policy_digest", None)
            data.pop("evidence_selection", None)
        return data

    @model_validator(mode="after")
    def reference_identity(self):
        if self.checkpoint_version == 1:
            if (
                self.disclosure_policy_digest is not None
                or self.evidence_selection is not None
            ):
                raise ValueError("v1 must not invent disclosure facts")
        elif self.disclosure_policy_digest is None or self.evidence_selection is None:
            raise ValueError("v2 requires disclosure and selection identities")
        if any(r.evidence_version != self.checkpoint_version for r in self.references):
            raise ValueError("evidence and manifest versions differ")
        if self.included_evidence_count != len(self.references) or (
            digest_arguments(
                {"references": [r.model_dump(mode="json") for r in self.references]}
            )
            != self.evidence_reference_digest
        ):
            raise ValueError("evidence references differ")
        return self
