"""Independent authorization of derived summaries; never executable authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from agentflow.application.ports.durable_summary import SummaryClaim, Version, canonical
from agentflow.application.ports.evidence_checkpoint import Digest
from agentflow.domain.agent.action import digest_arguments
from agentflow.domain.workflow.spec import Name

TOOL_NAME = "read_verified_summary"
Uncertainty = Literal["evidence_linked", "inference", "unknown"]


class SummaryConsumptionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["disabled", "verified_tool"] = "disabled"
    consumption_version: Version = 1
    source_summary_step: Name = "summary"
    max_consumed_claims: Annotated[int, Field(ge=1, le=32, strict=True)] = 8
    max_consumption_bytes: Annotated[int, Field(ge=256, le=4096, strict=True)] = 4096
    minimum_selected_evidence_coverage: Annotated[
        float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    ] = 1.0
    allowed_uncertainty: Annotated[
        tuple[Uncertainty, ...], Field(min_length=1, max_length=3)
    ] = ("evidence_linked",)
    allow_claim_text: bool = Field(default=False, strict=True)
    require_referenced_claims: bool = Field(default=True, strict=True)
    allowed_summary_versions: Annotated[
        tuple[Version, ...], Field(min_length=1, max_length=1)
    ] = (1,)

    @model_validator(mode="after")
    def unique_uncertainty(self):
        if len(set(self.allowed_uncertainty)) != len(self.allowed_uncertainty):
            raise ValueError("duplicate uncertainty categories")
        return self

    @property
    def digest(self):
        return digest_arguments(self.model_dump(mode="json"))


class ConsumedSummaryClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: Name
    text: Annotated[str, Field(min_length=1, max_length=2000, strict=True)] | None = (
        None
    )
    evidence_references: Annotated[tuple[Digest, ...], Field(max_length=32)]
    uncertainty: Uncertainty
    source_attribution_status: Literal["referenced", "unattributed"]
    claim_digest: Digest

    @model_serializer(mode="wrap")
    def metadata_shape(self, handler):
        data = handler(self)
        if self.text is None:
            data.pop("text", None)
        return data

    @classmethod
    def from_claim(cls, claim: SummaryClaim, *, text: bool):
        data = claim.model_dump(mode="json")
        return cls(
            **{
                **data,
                "text": claim.text if text else None,
                "claim_id": claim.claim_id
                if text
                else "claim-" + digest_arguments(data),
            },
            claim_digest=digest_arguments(data),
        )


class VerifiedSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    consumption_version: Version = 1
    summary_artifact_digest: Digest
    summary_request_id: Digest
    summary_version: Version = 1
    source_snapshot_digest: Digest
    consumption_policy_digest: Digest
    selected_claims: Annotated[
        tuple[ConsumedSummaryClaim, ...], Field(min_length=1, max_length=32)
    ]
    omitted_claim_count: Annotated[int, Field(ge=0, strict=True)]
    selected_evidence_coverage: Annotated[
        float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    ]
    summary_evidence_coverage: Annotated[
        float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    ]
    source_completeness: Literal["selected_evidence_only"] = "selected_evidence_only"
    source_omitted_evidence_count: Annotated[int, Field(ge=0, strict=True)]
    selected_truncated_evidence_count: Annotated[int, Field(ge=0, strict=True)]
    assurance: Literal["generated_not_fact_verified"] = "generated_not_fact_verified"
    view_digest: Digest

    @model_validator(mode="after")
    def valid_identity(self):
        if (
            self.selected_evidence_coverage > self.summary_evidence_coverage
            or self.view_digest
            != digest_arguments(self.model_dump(mode="json", exclude={"view_digest"}))
        ):
            raise ValueError("summary view digest differs")
        return self


class SummaryConsumptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: SummaryConsumptionPolicy
    view: VerifiedSummaryView

    @model_validator(mode="after")
    def authorized_view(self):
        p, v = self.policy, self.view
        if (
            p.mode != "verified_tool"
            or v.consumption_policy_digest != p.digest
            or v.summary_version not in p.allowed_summary_versions
            or len(v.selected_claims) > p.max_consumed_claims
            or v.selected_evidence_coverage < p.minimum_selected_evidence_coverage
            or any(
                c.uncertainty not in p.allowed_uncertainty
                or (p.require_referenced_claims and not c.evidence_references)
                or (not p.allow_claim_text and c.text is not None)
                for c in v.selected_claims
            )
            or len(canonical(v.model_dump(mode="json")).encode("utf-8"))
            > p.max_consumption_bytes
        ):
            raise ValueError("summary view is not authorized")
        return self
