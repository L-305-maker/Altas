"""Context identity contracts. No prompt or tool-result content is persisted here."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_serializer,
    model_validator,
)

from agentflow.application.ports.context_egress import ProviderContextEgressManifest
from agentflow.application.ports.evidence_checkpoint import EvidenceCheckpointManifest

Positive = Annotated[int, Field(ge=1, strict=True)]
Count = Annotated[int, Field(ge=0, strict=True)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Selection = Literal["full", "recent_complete_rounds", "evidence_anchored_recent_rounds"]


class ContextProjectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Selection = "full"
    projection_version: Annotated[int, Field(ge=1, le=1, strict=True)] = 1
    min_recent_rounds: Positive = 1


class ContextProjectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Readers can retain other versions; this implementation only produces v1.
    projection_version: Positive
    selection_policy: Selection
    min_recent_rounds: Positive
    max_context_bytes: Positive
    full_context_digest: Digest
    projected_context_digest: Digest
    projected_context_bytes: Positive
    included_round_count: Count
    omitted_round_count: Count
    tool_schema_digest: Digest
    checkpoint: EvidenceCheckpointManifest | None = None
    egress: ProviderContextEgressManifest | None = None

    @model_serializer(mode="wrap")
    def legacy_shape(self, handler):
        data = handler(self)
        if self.checkpoint is None:
            data.pop("checkpoint", None)
        if self.egress is None:
            data.pop("egress", None)
        return data

    @model_validator(mode="after")
    def consistent_window(self):
        if (self.selection_policy == "evidence_anchored_recent_rounds") != (
            self.checkpoint is not None
        ):
            raise ValueError("checkpoint and selection policy must agree")
        if self.projected_context_bytes > self.max_context_bytes:
            raise ValueError("projected context exceeds its limit")
        if self.omitted_round_count and (
            self.selection_policy == "full"
            or self.included_round_count < self.min_recent_rounds
        ):
            raise ValueError("projection must preserve the required suffix")
        return self


class ContextProjectionReservation(BaseModel):
    """Transient schemas allow Store to recompute the manifest before admission.

    Only manifest is stored on the attempt; schemas and messages are not copied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest: ContextProjectionManifest
    tools: tuple[dict[str, JsonValue], ...] = ()


class ContextProjectionError(Exception):
    def __init__(self, code: str = "AgentContextProjectionInvalid"):
        self.code = code
        super().__init__(code)
