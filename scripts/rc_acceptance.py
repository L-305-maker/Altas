"""Offline RC evidence verification, not a runtime ledger or release authority.

Only pinned Ed25519 identities can attest facts. Infrastructure attestations are
separate from host keys; cryptography cannot establish physical independence.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator
from rc_staging import DeploymentIdentity, Digest, Frozen

from agentflow.application.ports.model_request import canonical_json, request_digest

CaseId = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
Result = Literal["PASS", "FAIL", "NOT_VERIFIED"]
Epoch = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
Count = Annotated[int, Field(ge=0, le=1_000_000, strict=True)]
StepStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting",
    "retrying",
    "succeeded",
    "failed",
    "cancelled",
]
AgentStatus = Literal[
    "ready_model",
    "model_inflight",
    "actions_pending",
    "waiting_approval",
    "waiting_reconciliation",
    "succeeded",
    "failed",
    "exhausted",
    "cancelled",
]
Failure = Literal[
    "LeaseOverlap",
    "RevisionRollback",
    "BudgetRollback",
    "PersistentHistoryChanged",
    "CompletedRedispatched",
    "UnknownRedispatched",
    "ApprovalChanged",
    "PermitDuplicate",
    "ArtifactDuplicate",
    "CancelledLateCommit",
    "WaitingInflight",
    "StepAgentMismatch",
    "RetryBeforeDeadline",
    "AdmissionQuotaExceeded",
    "StaleCommitAccepted",
    "InvalidPersistentState",
    "ReviewRejected",
    "BuildMismatch",
    "InfrastructureMismatch",
]
Limitation = Literal[
    "DatabaseBeforeMissing",
    "DatabaseAfterMissing",
    "BuildEvidenceMissing",
    "InfrastructureEvidenceMissing",
    "TwoHostEvidenceMissing",
    "FaultPhaseMissing",
    "IndependentReviewMissing",
    "ScenarioWitnessMissing",
    "LiveDatabaseNotVerified",
    "ProviderNotVerified",
    "LiveTokenConformanceNotVerified",
    "ReleaseAuthorizationRequired",
    "PostgresEvidenceMissing",
]
PHASES = {
    "A": "lease_competition",
    "B": "worker_exit_inflight",
    "C": "partition_before_takeover",
    "D": "consumed_approval_restart",
    "E": "unknown_before_reconciliation",
    "F": "shared_admission_competition",
    "G": "rate_limit_before_deadline",
    "H": "summary_recovery",
    "I": "cancel_before_late_result",
    "J": "database_disconnect",
}
FaultPhase = Literal[
    "lease_competition",
    "worker_exit_inflight",
    "partition_before_takeover",
    "consumed_approval_restart",
    "unknown_before_reconciliation",
    "shared_admission_competition",
    "rate_limit_before_deadline",
    "summary_recovery",
    "cancel_before_late_result",
    "database_disconnect",
]


class AcceptanceChallenge(Frozen):
    evidence_version: Literal[2] = 2
    environment_id: UUID
    deployment_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    application_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    alembic_revision: Annotated[str, Field(pattern=r"^\d{4}$")]
    database_identity_digest: Digest
    staging_batch_digest: Digest
    v1_report_digest: Digest
    execution_nonce: Digest
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < (self.expires_at - self.issued_at).total_seconds() <= 3600:
            raise ValueError("InvalidChallengeTime")
        return self


class TrustedSigner(Frozen):
    key_id: Digest
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    role: Literal["database", "build", "host", "infrastructure", "fault", "reviewer"]
    host_digest: Digest | None = None
    not_before: AwareDatetime
    not_after: AwareDatetime

    @model_validator(mode="after")
    def identity(self):
        if (
            self.key_id != request_digest(self.public_key)
            or self.not_after <= self.not_before
        ):
            raise ValueError("InvalidTrustIdentity")
        if (self.role == "host") != (self.host_digest is not None):
            raise ValueError("InvalidHostBinding")
        return self


class AcceptanceTrust(Frozen):
    version: int = Field(strict=True, ge=1)
    signers: tuple[TrustedSigner, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def distinct(self):
        if len({s.key_id for s in self.signers}) != len(self.signers):
            raise ValueError("DuplicateTrustIdentity")
        hosts = [s.host_digest for s in self.signers if s.role == "host"]
        if len(set(hosts)) != len(hosts):
            raise ValueError("DuplicateHostIdentity")
        return self


class StepFact(Frozen):
    ref: Digest
    run_ref: Digest
    run_status: Literal[
        "pending", "running", "waiting", "succeeded", "failed", "cancelled"
    ]
    status: StepStatus
    agent_status: AgentStatus | None = None
    revision: Count | None = None
    counters: tuple[Count, Count, Count, Count] = (0, 0, 0, 0)
    attempts: Count = 0
    lease_digest: Digest | None = None
    lease_until: Epoch | None = None
    available_at: Epoch = 0.0
    deferred_retry: bool = Field(default=False, strict=True)
    continuation: bool = Field(default=False, strict=True)
    summary_identity: Digest | None = None
    consumer_identity: Digest | None = None
    artifact_required: bool = Field(default=False, strict=True)
    agent_identity: Digest | None = None
    snapshot_digest: Digest | None = None


class ActionFact(Frozen):
    ref: Digest
    step_ref: Digest
    status: Literal["proposed", "inflight", "succeeded", "failed", "unknown", "denied"]
    identity_digest: Digest
    consumed_at: Epoch | None = None
    reconciled_at: Epoch | None = None
    outcome_digest: Digest | None = None


class AttemptFact(Frozen):
    ref: Digest
    step_ref: Digest
    call_ref: Digest
    index: Count
    outcome: Literal["inflight", "succeeded", "failed", "unknown", "LEGACY_UNMEASURED"]
    identity_digest: Digest
    created_at: Epoch | None = None
    provider_ref: Digest | None = None
    outcome_digest: Digest | None = None


class PermitFact(Frozen):
    ref: Digest
    attempt_ref: Digest
    provider_ref: Digest
    status: Literal["active", "released", "unknown"]
    created_at: Epoch
    expires_at: Epoch
    released_at: Epoch | None = None


class ProviderFact(Frozen):
    ref: Digest
    max_concurrent: Count
    max_requests: Count
    window_seconds: Epoch
    cooldown_until: Epoch


class ArtifactFact(Frozen):
    step_ref: Digest
    digest: Digest


class DatabaseSnapshot(Frozen):
    observed_at: AwareDatetime
    database_backend: Literal["postgresql", "sqlite", "unverified"] = "unverified"
    steps: tuple[StepFact, ...] = Field(default=(), max_length=100)
    actions: tuple[ActionFact, ...] = Field(default=(), max_length=2000)
    attempts: tuple[AttemptFact, ...] = Field(default=(), max_length=2000)
    permits: tuple[PermitFact, ...] = Field(default=(), max_length=2000)
    providers: tuple[ProviderFact, ...] = Field(default=(), max_length=16)
    artifacts: tuple[ArtifactFact, ...] = Field(default=(), max_length=100)
    invalid_records: Count = 0


class DatabaseEvidence(Frozen):
    source: Literal["database"] = "database"
    case_id: CaseId
    phase: Literal["before", "after"]
    snapshot: DatabaseSnapshot


class BuildEvidence(Frozen):
    source: Literal["build"] = "build"
    identity: DeploymentIdentity


class InfrastructureEvidence(Frozen):
    source: Literal["infrastructure"] = "infrastructure"
    inventory_digest: Digest
    # Infrastructure authority maps host receipt identities to physical failure
    # domains. This assertion needs an independently reviewed asset inventory.
    host_failure_domains: tuple[tuple[Digest, Digest], ...] = Field(
        min_length=2, max_length=3
    )


class Operation(Frozen):
    kind: Literal[
        "claim",
        "dispatch",
        "unknown_dispatch",
        "stale_commit",
        "cancelled_commit",
        "resume",
        "model_reserve",
        "rate_limited",
        "database_unavailable",
        "worker_exit",
        "database_recovered",
    ]
    target: Digest
    outcome: Literal["accepted", "rejected", "unavailable", "safe_stopped"]
    at: Epoch
    lease_until: Epoch | None = None
    error_code: Literal["LostLease", "StateConflict", "DatabaseUnavailable"] | None = (
        None
    )


class HostEvidence(Frozen):
    source: Literal["host"] = "host"
    case_id: CaseId
    host_digest: Digest
    operations: tuple[Operation, ...] = Field(max_length=200)


class FaultEvidence(Frozen):
    source: Literal["fault"] = "fault"
    case_id: CaseId
    phase: FaultPhase
    protected_record_digest: Digest


class ReviewEvidence(Frozen):
    source: Literal["reviewer"] = "reviewer"
    case_id: CaseId
    reviewed_evidence_digest: Digest
    review_status: Literal["approved", "rejected"]


Payload = Annotated[
    DatabaseEvidence
    | BuildEvidence
    | InfrastructureEvidence
    | HostEvidence
    | FaultEvidence
    | ReviewEvidence,
    Field(discriminator="source"),
]


class AcceptanceEvidence(Frozen):
    evidence_version: Literal[2] = 2
    challenge: AcceptanceChallenge
    attestation_identity: Digest
    started_at: AwareDatetime
    completed_at: AwareDatetime
    payload: Payload

    @model_validator(mode="after")
    def time_range(self):
        if (
            not self.challenge.issued_at
            <= self.started_at
            <= self.completed_at
            <= self.challenge.expires_at
        ):
            raise ValueError("InvalidEvidenceTime")
        if (
            isinstance(self.payload, DatabaseEvidence)
            and not self.started_at
            <= self.payload.snapshot.observed_at
            <= self.completed_at
        ):
            raise ValueError("InvalidSnapshotTime")
        if isinstance(self.payload, HostEvidence) and any(
            not self.started_at.timestamp() <= op.at <= self.completed_at.timestamp()
            for op in self.payload.operations
        ):
            raise ValueError("InvalidOperationTime")
        return self


class SignedEvidence(Frozen):
    evidence: AcceptanceEvidence
    evidence_digest: Digest
    signature: Annotated[str, Field(pattern=r"^[a-f0-9]{128}$")]


class CaseAcceptance(Frozen):
    case_id: CaseId
    status: Result
    observed_invariants: tuple[Failure, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    evidence_digest: Digest


class RCIndependentAcceptance(Frozen):
    schema_version: Literal[2] = 2
    challenge_digest: Digest
    v1_report_digest: Digest
    trust_version: int = Field(strict=True, ge=1)
    cases: tuple[CaseAcceptance, ...]
    live_database_status: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    provider_acceptance: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    token_conformance: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    production_release_status: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    limitations: tuple[Limitation, ...] = (
        "LiveDatabaseNotVerified",
        "ProviderNotVerified",
        "LiveTokenConformanceNotVerified",
        "ReleaseAuthorizationRequired",
    )


def signing_bytes(evidence):
    # Domain separation prevents reusing a signature from another JSON protocol.
    return b"Altas.RCIndependentEvidence.v2\n" + canonical_json(
        evidence.model_dump(mode="json")
    ).encode("utf-8")


def evidence_set_digest(receipts):
    return request_digest(sorted(r.evidence_digest for r in receipts))


def snapshot_failures(snapshot):
    failures = set()
    if snapshot.invalid_records:
        failures.add("InvalidPersistentState")
    for field in ("steps", "actions", "attempts", "permits", "providers"):
        records = getattr(snapshot, field)
        if len({r.ref for r in records}) != len(records):
            failures.add(
                "PermitDuplicate" if field == "permits" else "InvalidPersistentState"
            )
    if len({p.attempt_ref for p in snapshot.permits}) != len(snapshot.permits):
        failures.add("PermitDuplicate")
    if len({a.step_ref for a in snapshot.artifacts}) != len(snapshot.artifacts):
        failures.add("ArtifactDuplicate")
    steps = {s.ref: s for s in snapshot.steps}
    for step in snapshot.steps:
        terminal = {
            "succeeded": "succeeded",
            "failed": "failed",
            "exhausted": "failed",
            "cancelled": "cancelled",
        }
        if step.agent_status in terminal and step.status != terminal[step.agent_status]:
            failures.add("StepAgentMismatch")
        if step.agent_status is not None:
            if step.status in terminal and step.agent_status not in terminal:
                failures.add("StepAgentMismatch")
            if (step.status == "waiting") != (
                step.agent_status in {"waiting_approval", "waiting_reconciliation"}
            ):
                failures.add("StepAgentMismatch")
        if step.run_status == "cancelled" and step.status not in terminal:
            failures.add("CancelledLateCommit")
        if step.status != "running" and (
            step.lease_digest is not None or step.lease_until is not None
        ):
            failures.add("InvalidPersistentState")
        if step.status == "running" and (
            step.lease_digest is None or step.lease_until is None
        ):
            failures.add("InvalidPersistentState")
        if step.run_status == "succeeded" and step.status != "succeeded":
            failures.add("StepAgentMismatch")
        if step.deferred_retry and (
            step.status != "ready"
            or step.agent_status != "ready_model"
            or not step.continuation
        ):
            failures.add("InvalidPersistentState")
        if sum(a.step_ref == step.ref for a in snapshot.artifacts) != int(
            step.status == "succeeded" and step.artifact_required
        ):
            failures.add("ArtifactDuplicate")
    for action in snapshot.actions:
        step = steps.get(action.step_ref)
        if step is None:
            failures.add("InvalidPersistentState")
        elif step.status == "waiting" and action.status == "inflight":
            failures.add("WaitingInflight")
    if any(a.step_ref not in steps for a in snapshot.attempts + snapshot.artifacts):
        failures.add("InvalidPersistentState")
    if any(
        p.provider_ref not in {r.ref for r in snapshot.providers}
        for p in snapshot.permits
    ):
        failures.add("InvalidPersistentState")
    for provider in snapshot.providers:
        permits = [p for p in snapshot.permits if p.provider_ref == provider.ref]
        # Recompute historical reservations, not process-local metric counters.
        for permit in permits:
            at = permit.created_at
            active = sum(
                p.created_at
                <= at
                < min(
                    p.expires_at,
                    p.released_at if p.released_at is not None else p.expires_at,
                )
                for p in permits
            )
            recent = sum(
                at - provider.window_seconds < p.created_at <= at for p in permits
            )
            if active > provider.max_concurrent or recent > provider.max_requests:
                failures.add("AdmissionQuotaExceeded")
    return tuple(sorted(failures))


def transition_failures(before, after, *, allow_initialization=False):
    failures = set(snapshot_failures(before)) | set(snapshot_failures(after))
    if after.observed_at < before.observed_at:
        failures.add("InvalidPersistentState")
    old_steps, new_steps = (
        {s.ref: s for s in before.steps},
        {s.ref: s for s in after.steps},
    )
    if old_steps.keys() != new_steps.keys():
        failures.add("PersistentHistoryChanged")
    for ref, old in old_steps.items():
        current = new_steps.get(ref)
        if current is None:
            continue
        if old.revision is not None and (
            current.revision is None or current.revision < old.revision
        ):
            failures.add("RevisionRollback")
        initialized = (
            allow_initialization
            and old.agent_identity is None
            and old.agent_status is None
            and old.revision is None
            and old.snapshot_digest is None
            and old.counters == (0, 0, 0, 0)
            and not any(a.step_ref == ref for a in before.actions + before.attempts)
            and current.agent_identity is not None
            and current.revision is not None
        )
        if (old.agent_identity != current.agent_identity and not initialized) or (
            old.revision == current.revision
            and old.snapshot_digest != current.snapshot_digest
        ):
            failures.add("PersistentHistoryChanged")
        if any(b < a for a, b in zip(old.counters, current.counters, strict=True)):
            failures.add("BudgetRollback")
        if old.run_status == "cancelled" and current != old:
            failures.add("CancelledLateCommit")
        if (
            old.summary_identity is not None
            and old.summary_identity != current.summary_identity
        ):
            failures.add("PersistentHistoryChanged")
        if (
            old.consumer_identity is not None
            and old.consumer_identity != current.consumer_identity
        ):
            failures.add("PersistentHistoryChanged")
    for field in ("actions", "attempts"):
        new = {r.ref: r for r in getattr(after, field)}
        for old in getattr(before, field):
            current = new.get(old.ref)
            if current is None or current.identity_digest != old.identity_digest:
                failures.add("PersistentHistoryChanged")
                continue
            if field == "actions":
                if old.status in {"succeeded", "failed", "denied"} and current != old:
                    failures.add("CompletedRedispatched")
                if old.status == "unknown" and (
                    current.status in {"proposed", "inflight"}
                    or (current.status != "unknown" and current.reconciled_at is None)
                ):
                    failures.add("UnknownRedispatched")
                if (
                    old.consumed_at is not None
                    and current.consumed_at != old.consumed_at
                ):
                    failures.add("ApprovalChanged")
            elif old.outcome != "inflight" and current != old:
                failures.add("PersistentHistoryChanged")
    old_attempts = {a.ref for a in before.attempts}
    unknown_calls = {a.call_ref for a in before.attempts if a.outcome == "unknown"}
    for attempt in after.attempts:
        step = old_steps.get(attempt.step_ref)
        if (
            attempt.ref not in old_attempts
            and step
            and step.deferred_retry
            and (attempt.created_at is None or attempt.created_at < step.available_at)
        ):
            failures.add("RetryBeforeDeadline")
        if attempt.ref not in old_attempts and step and step.run_status == "cancelled":
            failures.add("CancelledLateCommit")
        if attempt.ref not in old_attempts and attempt.call_ref in unknown_calls:
            failures.add("UnknownRedispatched")
        if attempt.ref not in old_attempts and any(
            p.ref == attempt.provider_ref
            and p.cooldown_until > (attempt.created_at or 0)
            for p in before.providers
        ):
            failures.add("RetryBeforeDeadline")
    if not set(before.artifacts) <= set(after.artifacts):
        failures.add("PersistentHistoryChanged")
    permits = {p.ref: p for p in after.permits}
    for prior in before.permits:
        current = permits.get(prior.ref)
        if (
            current is None
            or current.model_dump(exclude={"status", "released_at"})
            != prior.model_dump(exclude={"status", "released_at"})
            or (prior.status != "active" and current != prior)
        ):
            failures.add("PersistentHistoryChanged")
    return failures


def scenario_witness(case, before, after, operations):
    old = {a.ref: a for a in before.actions}
    actions = [a for a in after.actions if a.ref in old]
    kinds = {o.kind for o in operations}
    old_steps = {s.ref: s for s in before.steps}
    new_steps = {s.ref: s for s in after.steps}
    resumed = {o.target for o in operations if o.kind == "resume"}
    if case == "A":
        return sum(o.kind == "claim" for o in operations) >= 2 and any(
            o.kind == "claim"
            and o.outcome == "accepted"
            and o.target in old_steps
            and old_steps[o.target].status == "ready"
            and o.target in new_steps
            and new_steps[o.target].status == "running"
            and new_steps[o.target].lease_until == o.lease_until
            for o in operations
        )
    if case == "B":
        return any(
            old[a.ref].status == "inflight"
            and a.status == "unknown"
            and new_steps[a.step_ref].agent_status == "waiting_reconciliation"
            for a in actions
            if a.step_ref in new_steps
        )
    if case == "C":
        return any(
            o.kind == "stale_commit"
            and o.outcome == "rejected"
            and o.error_code == "LostLease"
            and o.target in old_steps
            and o.target in new_steps
            and old_steps[o.target].lease_digest is not None
            and new_steps[o.target].lease_digest is not None
            and old_steps[o.target].lease_digest != new_steps[o.target].lease_digest
            for o in operations
        )
    if case == "D":
        return any(
            a.consumed_at is not None
            and a == old[a.ref]
            and a.status in {"succeeded", "failed"}
            and a.step_ref in resumed
            for a in actions
        )
    if case == "E":
        return any(
            old[a.ref].status == "unknown"
            and a.status in {"succeeded", "failed"}
            and a.reconciled_at is not None
            and a.step_ref in resumed
            and before.observed_at.timestamp()
            <= a.reconciled_at
            <= after.observed_at.timestamp()
            for a in actions
        )
    if case == "F":
        old_permits = {p.ref for p in before.permits}
        attempts = {a.ref: a for a in after.attempts}
        reserved = {
            o.target
            for o in operations
            if o.kind == "model_reserve" and o.outcome == "accepted"
        }
        return (
            bool(after.providers)
            and any(
                p.ref not in old_permits
                and p.attempt_ref in attempts
                and attempts[p.attempt_ref].step_ref in reserved
                for p in after.permits
            )
            and sum(o.kind == "model_reserve" for o in operations) >= 2
        )
    if case == "G":
        old_calls = {a.call_ref for a in before.attempts if a.outcome == "failed"}
        old_refs = {a.ref for a in before.attempts}
        deadlines = [
            s.available_at
            for s in before.steps
            if s.deferred_retry and s.available_at > before.observed_at.timestamp()
        ]
        return (
            "rate_limited" in kinds
            and bool(deadlines)
            and any(
                a.ref not in old_refs
                and a.call_ref in old_calls
                and a.created_at is not None
                and a.created_at >= max(deadlines, default=0)
                for a in after.attempts
            )
        )
    if case == "H":
        return (
            "resume" in kinds
            and any(s.summary_identity for s in after.steps)
            and any(s.consumer_identity for s in after.steps)
            and bool(after.artifacts)
            and all(a.identity_digest for a in after.attempts)
        )
    if case == "I":
        return any(
            o.kind == "cancelled_commit"
            and o.outcome == "rejected"
            and o.error_code in {"LostLease", "StateConflict"}
            and o.target in new_steps
            and new_steps[o.target].status == "cancelled"
            and new_steps[o.target].run_status == "cancelled"
            for o in operations
        )
    return any(
        o.kind == "database_unavailable" and o.outcome == "safe_stopped"
        for o in operations
    ) and bool(before.steps and after.steps)


def verify_acceptance(challenge, trust, receipts, *, now=None):
    if challenge.evidence_version != 2:
        raise ValueError("EvidenceVersionMismatch")
    return _verify_acceptance(challenge, trust, receipts, now=now)


def _verify_acceptance(
    challenge,
    trust,
    receipts,
    *,
    now=None,
    signing=signing_bytes,
    witness=scenario_witness,
    report_type=RCIndependentAcceptance,
    causal_check=None,
    transition_check=transition_failures,
):
    now = now or datetime.now(timezone.utc)
    if not challenge.issued_at <= now <= challenge.expires_at:
        raise ValueError("EvidenceExpired")
    if len(receipts) > 100:
        raise ValueError("EvidenceBoundsExceeded")
    signers, seen = {s.key_id: s for s in trust.signers}, set()
    for receipt in receipts:
        evidence = receipt.evidence
        if evidence.challenge != challenge:
            raise ValueError("EvidenceScopeMismatch")
        if receipt.evidence_digest in seen:
            raise ValueError("EvidenceReplay")
        seen.add(receipt.evidence_digest)
        if receipt.evidence_digest != request_digest(evidence.model_dump(mode="json")):
            raise ValueError("EvidenceDigestMismatch")
        signer = signers.get(evidence.attestation_identity)
        if signer is None or signer.role != evidence.payload.source:
            raise ValueError("UntrustedEvidenceSigner")
        if (
            not signer.not_before
            <= evidence.started_at
            <= evidence.completed_at
            <= min(now, signer.not_after)
            or now > signer.not_after
        ):
            raise ValueError("EvidenceExpired")
        if (
            isinstance(evidence.payload, HostEvidence)
            and evidence.payload.host_digest != signer.host_digest
        ):
            raise ValueError("EvidenceHostMismatch")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer.public_key)).verify(
                bytes.fromhex(receipt.signature), signing(evidence)
            )
        except InvalidSignature:
            raise ValueError("EvidenceSignatureRejected") from None
    cases = []
    for case in PHASES:
        selected = [
            r for r in receipts if getattr(r.evidence.payload, "case_id", case) == case
        ]
        payloads = [r.evidence.payload for r in selected]
        failures, missing = set(), set()
        databases = [p for p in payloads if isinstance(p, DatabaseEvidence)]
        before = [p.snapshot for p in databases if p.phase == "before"]
        after = [p.snapshot for p in databases if p.phase == "after"]
        if len(before) > 1 or len(after) > 1:
            raise ValueError("AmbiguousDatabaseEvidence")
        for snapshot in before + after:
            failures.update(snapshot_failures(snapshot))
            if snapshot.database_backend != "postgresql":
                missing.add("PostgresEvidenceMissing")
        if not before:
            missing.add("DatabaseBeforeMissing")
        if not after:
            missing.add("DatabaseAfterMissing")
        builds = [p.identity for p in payloads if isinstance(p, BuildEvidence)]
        if len(builds) != 1:
            missing.add("BuildEvidenceMissing")
        elif (
            builds[0].git_commit_sha != challenge.deployment_sha
            or builds[0].application_version != challenge.application_version
        ):
            failures.add("BuildMismatch")
        infra = [p for p in payloads if isinstance(p, InfrastructureEvidence)]
        hosts = [p for p in payloads if isinstance(p, HostEvidence)]
        if len(infra) != 1:
            missing.add("InfrastructureEvidenceMissing")
        else:
            mapping = dict(infra[0].host_failure_domains)
            if (
                len(mapping) != len(infra[0].host_failure_domains)
                or len(set(mapping.values())) != len(mapping)
                or any(h.host_digest not in mapping for h in hosts)
            ):
                failures.add("InfrastructureMismatch")
        participation = {"A": "claim", "F": "model_reserve"}.get(case)
        if (
            len(
                {
                    h.host_digest
                    for h in hosts
                    if any(
                        participation is None or op.kind == participation
                        for op in h.operations
                    )
                }
            )
            < 2
        ):
            missing.add("TwoHostEvidenceMissing")
        faults = [r for r in selected if isinstance(r.evidence.payload, FaultEvidence)]
        if len(faults) != 1 or faults[0].evidence.payload.phase != PHASES[case]:
            missing.add("FaultPhaseMissing")
        elif (
            before
            and after
            and not before[0].observed_at
            <= faults[0].evidence.started_at
            <= faults[0].evidence.completed_at
            <= after[0].observed_at
        ):
            missing.add("FaultPhaseMissing")
        operations = [o for host in hosts for o in host.operations]
        if before and after:
            targets = {
                "step": {s.ref for s in before[0].steps + after[0].steps},
                "action": {a.ref for a in before[0].actions + after[0].actions},
            }
            prior_actions = {a.ref: a for a in before[0].actions}
            for operation in operations:
                expected = (
                    {challenge.database_identity_digest}
                    if operation.kind in {"database_unavailable", "database_recovered"}
                    else targets[
                        "action"
                        if operation.kind in {"dispatch", "unknown_dispatch"}
                        else "step"
                    ]
                )
                if operation.target not in expected:
                    failures.add("InvalidPersistentState")
                if (
                    not before[0].observed_at.timestamp()
                    <= operation.at
                    <= after[0].observed_at.timestamp()
                ):
                    failures.add("InvalidPersistentState")
                old_action = prior_actions.get(operation.target)
                if (
                    operation.kind == "dispatch"
                    and operation.outcome == "accepted"
                    and old_action
                ):
                    if old_action.status == "unknown":
                        failures.add("UnknownRedispatched")
                    if (
                        old_action.status in {"succeeded", "failed", "denied"}
                        or old_action.consumed_at is not None
                    ):
                        failures.add("CompletedRedispatched")
        for op in operations:
            if op.outcome == "accepted" and op.kind in {
                "stale_commit",
                "unknown_dispatch",
                "cancelled_commit",
            }:
                failures.add(
                    {
                        "stale_commit": "StaleCommitAccepted",
                        "unknown_dispatch": "UnknownRedispatched",
                        "cancelled_commit": "CancelledLateCommit",
                    }[op.kind]
                )
        claims = [
            o for o in operations if o.kind == "claim" and o.outcome == "accepted"
        ]
        for i, claim in enumerate(claims):
            if claim.lease_until is None or claim.lease_until <= claim.at:
                failures.add("InvalidPersistentState")
            elif any(
                other.target == claim.target
                and other.at < claim.lease_until
                and claim.at < (other.lease_until or other.at)
                for other in claims[i + 1 :]
            ):
                failures.add("LeaseOverlap")
        dispatched = [
            o.target
            for o in operations
            if o.kind == "dispatch" and o.outcome == "accepted"
        ]
        if len(dispatched) != len(set(dispatched)):
            failures.add("CompletedRedispatched")
        if before and after:
            failures.update(transition_check(before[0], after[0]))
            if not witness(case, before[0], after[0], operations):
                missing.add("ScenarioWitnessMissing")
        if causal_check is not None and before and after:
            bad, incomplete = causal_check(
                case, before[0], after[0], hosts, faults, challenge
            )
            failures.update(bad)
            missing.update(incomplete)
        review = [r for r in selected if isinstance(r.evidence.payload, ReviewEvidence)]
        reviewed = [
            r for r in selected if not isinstance(r.evidence.payload, ReviewEvidence)
        ]
        if (
            len(review) != 1
            or review[0].evidence.payload.reviewed_evidence_digest
            != evidence_set_digest(reviewed)
            or (
                reviewed
                and review[0].evidence.started_at
                < max(r.evidence.completed_at for r in reviewed)
            )
        ):
            missing.add("IndependentReviewMissing")
        elif review[0].evidence.payload.review_status != "approved":
            failures.add("ReviewRejected")
        cases.append(
            CaseAcceptance(
                case_id=case,
                status="FAIL" if failures else ("NOT_VERIFIED" if missing else "PASS"),
                observed_invariants=tuple(sorted(failures)),
                limitations=tuple(sorted(missing)),
                evidence_digest=evidence_set_digest(selected),
            )
        )
    return report_type(
        challenge_digest=request_digest(challenge.model_dump(mode="json")),
        v1_report_digest=challenge.v1_report_digest,
        trust_version=trust.version,
        cases=tuple(cases),
    )


def issue_challenge(directory, challenge):
    if os.name == "nt" and sys.version_info < (3, 12, 4):
        raise ValueError("PrivateChallengeRegistryRequired")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    _private_write(
        directory / "issued.json", canonical_json(challenge.model_dump(mode="json"))
    )


def _private_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def consume_challenge(directory, challenge):
    directory = Path(directory)
    if directory.is_symlink() or (os.name != "nt" and directory.stat().st_mode & 0o077):
        raise ValueError("PrivateChallengeRegistryRequired")
    issued = directory / "issued.json"
    if (
        issued.stat().st_size > 16000
        or issued.is_symlink()
        or type(challenge).model_validate_json(issued.read_bytes()) != challenge
    ):
        raise ValueError("ChallengeRegistrationMismatch")
    try:
        _private_write(
            directory / "consumed.json",
            request_digest(challenge.model_dump(mode="json")),
        )
    except FileExistsError:
        raise ValueError("ChallengeReplay") from None
