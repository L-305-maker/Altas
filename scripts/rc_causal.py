"""Version 3 causal acceptance. Legacy v2 serialization is deliberately unchanged."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator
from rc_acceptance import (
    AcceptanceChallenge,
    AcceptanceEvidence,
    ActionFact,
    AttemptFact,
    BuildEvidence,
    Count,
    DatabaseEvidence,
    DatabaseSnapshot,
    Digest,
    Epoch,
    FaultEvidence,
    Frozen,
    HostEvidence,
    InfrastructureEvidence,
    Operation,
    RCIndependentAcceptance,
    ReviewEvidence,
    SignedEvidence,
    _verify_acceptance,
    canonical_json,
    request_digest,
    transition_failures,
)


class CausalChallenge(AcceptanceChallenge):
    evidence_version: Literal[3] = 3


class CallFact(Frozen):
    ref: Digest
    step_ref: Digest
    round_index: Count
    attempts: Count
    status: Literal["inflight", "succeeded", "failed", "unknown"]


class ApprovalFact(Frozen):
    ref: Digest
    action_ref: Digest
    binding_digest: Digest
    decision: Literal["pending", "approved", "rejected", "cancelled"]
    consumed_at: Epoch | None = None


class ReconciliationFact(Frozen):
    ref: Digest
    action_ref: Digest
    previous_status: Literal["unknown"]
    confirmed_status: Literal["succeeded", "failed"]
    applied: bool = Field(strict=True)
    confirmed_at: Epoch


class SummaryLink(Frozen):
    source_step_ref: Digest
    generator_step_ref: Digest
    consumer_step_ref: Digest
    source_snapshot_digest: Digest
    summary_request_digest: Digest
    summary_input_digest: Digest
    artifact_digest: Digest
    view_digest: Digest
    generator_contract_digest: Digest
    consumer_contract_digest: Digest


class CausalAction(ActionFact):
    binding_digest: Digest | None = None
    idempotency_digest: Digest


class CausalAttempt(AttemptFact):
    contract_digest: Digest | None = None
    summary_input_digest: Digest | None = None
    summary_consumption_digest: Digest | None = None


class CausalSnapshot(DatabaseSnapshot):
    actions: tuple[CausalAction, ...] = Field(default=(), max_length=2000)
    attempts: tuple[CausalAttempt, ...] = Field(default=(), max_length=2000)
    calls: tuple[CallFact, ...] = Field(default=(), max_length=2000)
    approvals: tuple[ApprovalFact, ...] = Field(default=(), max_length=2000)
    reconciliations: tuple[ReconciliationFact, ...] = Field(default=(), max_length=2000)
    summary_links: tuple[SummaryLink, ...] = Field(default=(), max_length=100)
    # Explicit scope: other-Run permits are included, without pretending their
    # sessions belong to this batch. Overflow rejects collection, never truncates.
    global_provider_scope: tuple[Digest, ...] = Field(default=(), max_length=16)


class CausalDatabaseEvidence(DatabaseEvidence):
    snapshot: CausalSnapshot


class CausalFault(FaultEvidence):
    target_step_refs: tuple[Digest, ...] = Field(min_length=1, max_length=3)
    provider_ref: Digest | None = None
    competition_seconds: Annotated[float, Field(gt=0, le=60, strict=True)] = 5.0


class RCCausalWitness(Operation):
    evidence_version: Literal[3] = 3
    environment_id: UUID
    staging_batch_digest: Digest
    case_id: Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    causal_group_id: Digest
    target_step_ref: Digest
    action_ref: Digest | None = None
    model_call_ref: Digest | None = None
    attempt_ref: Digest | None = None
    provider_ref: Digest | None = None
    permit_ref: Digest | None = None
    approval_ref: Digest | None = None
    reconciliation_ref: Digest | None = None
    lease_digest: Digest | None = None
    host_identity: Digest
    database_before_digest: Digest
    database_after_digest: Digest
    fault_receipt_digest: Digest
    witness_digest: Digest
    error_code: (
        Literal[
            "LostLease",
            "StateConflict",
            "DatabaseUnavailable",
            "ProviderAdmissionDeferred",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def identity(self):
        if self.witness_digest != request_digest(
            self.model_dump(mode="json", exclude={"witness_digest"})
        ):
            raise ValueError("CausalWitnessDigestMismatch")
        return self


class CausalHost(HostEvidence):
    operations: tuple[RCCausalWitness, ...] = Field(max_length=200)


CausalPayload = Annotated[
    CausalDatabaseEvidence
    | BuildEvidence
    | InfrastructureEvidence
    | CausalHost
    | CausalFault
    | ReviewEvidence,
    Field(discriminator="source"),
]


class CausalEvidence(AcceptanceEvidence):
    evidence_version: Literal[3] = 3
    challenge: CausalChallenge
    payload: CausalPayload


class CausalSignedEvidence(SignedEvidence):
    evidence: CausalEvidence


class RCCausalAcceptance(RCIndependentAcceptance):
    schema_version: Literal[3] = 3


def signing_bytes(evidence):
    if evidence.evidence_version != 3:
        raise ValueError("EvidenceVersionMismatch")
    return b"Altas.RCCausalEvidence.v3\n" + canonical_json(
        evidence.model_dump(mode="json")
    ).encode("utf-8")


def causal_group(challenge, case, before, after, fault_digest):
    return request_digest(
        {
            "challenge": request_digest(challenge.model_dump(mode="json")),
            "case": case,
            "before": request_digest(before.model_dump(mode="json")),
            "after": request_digest(after.model_dump(mode="json")),
            "fault": fault_digest,
        }
    )


def relation_failures(snapshot):
    """Relations are facts, not inference from counts or unrelated successes."""
    bad = set()
    steps = {s.ref: s for s in snapshot.steps}
    actions = {a.ref: a for a in snapshot.actions}
    calls = {c.ref: c for c in snapshot.calls}
    attempts = {a.ref: a for a in snapshot.attempts}
    for records in (snapshot.calls, snapshot.approvals, snapshot.reconciliations):
        if len({r.ref for r in records}) != len(records):
            bad.add("InvalidPersistentState")
    for call in calls.values():
        if call.step_ref not in steps or call.round_index < 1 or call.attempts < 1:
            bad.add("InvalidPersistentState")
    for attempt in attempts.values():
        call = calls.get(attempt.call_ref)
        if (
            not call
            or call.step_ref != attempt.step_ref
            or attempt.index > call.attempts
        ):
            bad.add("InvalidPersistentState")
    for permit in snapshot.permits:
        attempt = attempts.get(permit.attempt_ref)
        if attempt and permit.provider_ref != attempt.provider_ref:
            bad.add("InvalidPersistentState")
        # Global permits of other Runs intentionally have no in-batch Attempt.
        if not attempt and permit.provider_ref not in snapshot.global_provider_scope:
            bad.add("InvalidPersistentState")
    for approval in snapshot.approvals:
        action = actions.get(approval.action_ref)
        if (
            not action
            or action.binding_digest != approval.binding_digest
            or action.consumed_at != approval.consumed_at
            or (approval.consumed_at is not None and approval.decision != "approved")
        ):
            bad.add("ApprovalChanged")
    for reconciliation in snapshot.reconciliations:
        action = actions.get(reconciliation.action_ref)
        if not action or (
            reconciliation.applied
            and (
                action.status != reconciliation.confirmed_status
                or action.reconciled_at != reconciliation.confirmed_at
            )
        ):
            bad.add("InvalidPersistentState")
    applied = [r.action_ref for r in snapshot.reconciliations if r.applied]
    if len(applied) != len(set(applied)):
        bad.add("InvalidPersistentState")
    for link in snapshot.summary_links:
        source = steps.get(link.source_step_ref)
        generator = steps.get(link.generator_step_ref)
        consumer = steps.get(link.consumer_step_ref)
        if (
            not source
            or not generator
            or not consumer
            or len({source.run_ref, generator.run_ref, consumer.run_ref}) != 1
            or source.status != "succeeded"
            or generator.status != "succeeded"
            or generator.summary_identity != link.artifact_digest
            or consumer.consumer_identity != link.view_digest
            or not any(
                a.step_ref == generator.ref and a.digest == link.artifact_digest
                for a in snapshot.artifacts
            )
            or not any(
                a.step_ref == generator.ref
                and a.contract_digest == link.generator_contract_digest
                and a.summary_input_digest == link.summary_input_digest
                for a in snapshot.attempts
            )
            or not any(
                a.step_ref == consumer.ref
                and a.contract_digest == link.consumer_contract_digest
                and a.summary_consumption_digest == link.view_digest
                for a in snapshot.attempts
            )
        ):
            bad.add("InvalidPersistentState")
    return bad


def _scenario(case, before, after, ops, fault, window_start):
    old_steps = {s.ref: s for s in before.steps}
    steps = {s.ref: s for s in after.steps}
    old_actions = {a.ref: a for a in before.actions}
    actions = {a.ref: a for a in after.actions}
    attempts = {a.ref: a for a in after.attempts}
    old_attempts = {a.ref: a for a in before.attempts}
    permits = {p.ref: p for p in after.permits}
    providers = {p.ref: p for p in after.providers}
    resumes = [o for o in ops if o.kind == "resume" and o.outcome == "accepted"]
    if case == "A":
        for win in ops:
            step = steps[win.target_step_ref]
            if (
                win.kind == "claim"
                and win.outcome == "accepted"
                and old_steps[step.ref].status == "ready"
                and step.status == "running"
                and win.lease_digest == step.lease_digest
                and win.lease_until == step.lease_until
                and any(
                    o.kind == "claim"
                    and o.outcome == "rejected"
                    and o.error_code is None
                    and o.lease_digest == win.lease_digest
                    and o.lease_until == win.lease_until
                    and o.target_step_ref == step.ref
                    and o.host_identity != win.host_identity
                    and win.at <= o.at < (win.lease_until or win.at)
                    and o.at - win.at <= fault.competition_seconds
                    for o in ops
                )
            ):
                return True
    elif case == "B":
        for op in ops:
            action = actions.get(op.action_ref)
            old = old_actions.get(op.action_ref)
            if (
                op.kind == "worker_exit"
                and op.outcome == "safe_stopped"
                and action
                and old
                and old.status == "inflight"
                and action.status == "unknown"
                and op.lease_digest == old_steps[action.step_ref].lease_digest
                and op.lease_digest is not None
                and op.at < (old_steps[action.step_ref].lease_until or 0)
                and steps[action.step_ref].agent_status == "waiting_reconciliation"
            ):
                return True
    elif case == "C":
        for op in ops:
            old, step = old_steps[op.target_step_ref], steps[op.target_step_ref]
            if (
                op.kind == "stale_commit"
                and op.outcome == "rejected"
                and op.error_code == "LostLease"
                and op.lease_digest == old.lease_digest
                and old.lease_digest is not None
                and step.lease_digest is not None
                and step.lease_digest != old.lease_digest
                and any(
                    o.kind == "claim"
                    and o.outcome == "accepted"
                    and o.target_step_ref == step.ref
                    and o.lease_digest == step.lease_digest
                    and o.lease_until == step.lease_until
                    and o.at <= op.at
                    and o.host_identity != op.host_identity
                    for o in ops
                )
            ):
                return True
    elif case == "D":
        for op in resumes:
            approval = next(
                (a for a in after.approvals if a.ref == op.approval_ref), None
            )
            old = next((a for a in before.approvals if a.ref == op.approval_ref), None)
            action = actions.get(op.action_ref)
            if (
                approval
                and approval == old
                and approval.action_ref == op.action_ref
                and approval.consumed_at is not None
                and action == old_actions.get(op.action_ref)
                and action.status in {"succeeded", "failed"}
                and not any(
                    o.kind == "dispatch" and o.action_ref == action.ref for o in ops
                )
            ):
                return True
    elif case == "E":
        for op in resumes:
            r = next(
                (r for r in after.reconciliations if r.ref == op.reconciliation_ref),
                None,
            )
            old, action = old_actions.get(op.action_ref), actions.get(op.action_ref)
            if (
                r
                and r.applied
                and r.action_ref == op.action_ref
                and old
                and action
                and old.status == "unknown"
                and action.status == r.confirmed_status
                and before.observed_at.timestamp() <= r.confirmed_at <= op.at
                and r.ref not in {r.ref for r in before.reconciliations}
            ):
                return True
    elif case == "F":
        for win in ops:
            a, p = attempts.get(win.attempt_ref), permits.get(win.permit_ref)
            pool = providers.get(win.provider_ref)
            if (
                win.kind != "model_reserve"
                or win.outcome != "accepted"
                or not a
                or not p
                or not pool
                or a.ref in old_attempts
                or p.ref in {p.ref for p in before.permits}
                or p.attempt_ref != a.ref
                or p.provider_ref != win.provider_ref
                or a.provider_ref != win.provider_ref
                or a.call_ref != win.model_call_ref
                or a.step_ref != win.target_step_ref
                or a.created_at is None
                or not window_start <= p.created_at <= a.created_at <= win.at
                or win.at - p.created_at > fault.competition_seconds
                or pool.ref != fault.provider_ref
                or pool.ref not in after.global_provider_scope
            ):
                continue
            for lose in ops:
                if (
                    lose.kind == "model_reserve"
                    and lose.outcome in {"rejected", "unavailable"}
                    and lose.host_identity != win.host_identity
                    and lose.provider_ref == pool.ref
                    and win.at <= lose.at <= win.at + fault.competition_seconds
                    and lose.error_code == "ProviderAdmissionDeferred"
                    and lose.attempt_ref is None
                    and lose.permit_ref is None
                    and not any(
                        a.step_ref == lose.target_step_ref and a.ref not in old_attempts
                        for a in after.attempts
                    )
                ):
                    active = sum(
                        p.created_at
                        <= lose.at
                        < min(p.expires_at, p.released_at or p.expires_at)
                        for p in after.permits
                        if p.provider_ref == pool.ref
                    )
                    recent = sum(
                        lose.at - pool.window_seconds < p.created_at <= lose.at
                        for p in after.permits
                        if p.provider_ref == pool.ref
                    )
                    if active >= pool.max_concurrent or recent >= pool.max_requests:
                        return True
    elif case == "G":
        for limited in ops:
            a = old_attempts.get(limited.attempt_ref)
            step = old_steps[limited.target_step_ref]
            pool = next(
                (p for p in before.providers if p.ref == limited.provider_ref), None
            )
            if (
                limited.kind == "rate_limited"
                and limited.outcome == "rejected"
                and a
                and a.outcome == "failed"
                and a.step_ref == step.ref
                and a.call_ref == limited.model_call_ref
                and a.provider_ref == limited.provider_ref
                and pool
                and step.deferred_retry
                and step.available_at > before.observed_at.timestamp()
                and pool.cooldown_until > before.observed_at.timestamp()
            ):
                if any(
                    o.kind == "model_reserve"
                    and o.outcome == "accepted"
                    and o.target_step_ref == step.ref
                    and o.model_call_ref == a.call_ref
                    and o.provider_ref == pool.ref
                    and o.attempt_ref in attempts
                    and o.attempt_ref not in old_attempts
                    and attempts[o.attempt_ref].call_ref == a.call_ref
                    and attempts[o.attempt_ref].provider_ref == pool.ref
                    and attempts[o.attempt_ref].step_ref == step.ref
                    and attempts[o.attempt_ref].index == a.index + 1
                    and attempts[o.attempt_ref].created_at is not None
                    and max(step.available_at, pool.cooldown_until)
                    <= attempts[o.attempt_ref].created_at
                    <= o.at
                    <= attempts[o.attempt_ref].created_at + fault.competition_seconds
                    for o in ops
                ):
                    return True
    elif case == "H":
        return any(
            o.target_step_ref == link.consumer_step_ref
            and set(
                (link.source_step_ref, link.generator_step_ref, link.consumer_step_ref)
            )
            <= set(fault.target_step_refs)
            for o in resumes
            for link in after.summary_links
        )
    elif case == "I":
        return any(
            o.kind == "cancelled_commit"
            and o.outcome == "rejected"
            and o.error_code in {"LostLease", "StateConflict"}
            and steps[o.target_step_ref].status == "cancelled"
            and steps[o.target_step_ref].run_status == "cancelled"
            and old_steps[o.target_step_ref].lease_digest == o.lease_digest
            and o.lease_digest is not None
            for o in ops
        )
    else:
        return any(
            o.kind == "database_unavailable"
            and o.outcome == "safe_stopped"
            and o.error_code == "DatabaseUnavailable"
            and any(
                r.kind == "database_recovered"
                and r.outcome == "accepted"
                and r.at > o.at
                and r.target_step_ref == o.target_step_ref
                for r in ops
            )
            for o in ops
        )
    return False


def check_causality(case, before, after, hosts, faults, challenge):
    failures = relation_failures(before) | relation_failures(after)
    missing = set()
    if len(faults) != 1:
        return failures, {"ScenarioWitnessMissing"}
    receipt = faults[0]
    fault = receipt.evidence.payload
    old_steps = {s.ref: s for s in before.steps}
    steps = {s.ref: s for s in after.steps}
    for ref in old_steps.keys() & steps.keys():
        if old_steps[ref].run_ref != steps[ref].run_ref:
            failures.add("PersistentHistoryChanged")
    for field, immutable in (
        ("actions", ("step_ref", "binding_digest", "idempotency_digest")),
        (
            "attempts",
            (
                "step_ref",
                "call_ref",
                "index",
                "provider_ref",
                "contract_digest",
                "summary_input_digest",
                "summary_consumption_digest",
                "created_at",
            ),
        ),
    ):
        current = {r.ref: r for r in getattr(after, field)}
        for old in getattr(before, field):
            new = current.get(old.ref)
            if new and any(
                getattr(new, name) != getattr(old, name) for name in immutable
            ):
                failures.add("PersistentHistoryChanged")
    for field in ("calls", "approvals", "reconciliations", "summary_links"):
        current = {
            getattr(r, "ref", getattr(r, "consumer_step_ref", None)): r
            for r in getattr(after, field)
        }
        for old in getattr(before, field):
            new = current.get(
                getattr(old, "ref", getattr(old, "consumer_step_ref", None))
            )
            if field == "calls":
                if (
                    new is None
                    or new.step_ref != old.step_ref
                    or new.round_index != old.round_index
                    or new.attempts < old.attempts
                    or (old.status in {"succeeded", "unknown"} and new != old)
                ):
                    failures.add("PersistentHistoryChanged")
            elif new != old:
                # Approval decisions/consumption can legitimately advance.
                if (
                    field != "approvals"
                    or new is None
                    or new.action_ref != old.action_ref
                    or new.binding_digest != old.binding_digest
                    or (old.consumed_at is not None and new != old)
                ):
                    failures.add("PersistentHistoryChanged")
    if not set(fault.target_step_refs) <= old_steps.keys() & steps.keys():
        return failures, {"ScenarioWitnessMissing"}
    group = causal_group(challenge, case, before, after, receipt.evidence_digest)
    ops = []
    for host in hosts:
        for op in host.operations:
            if (
                op.environment_id != challenge.environment_id
                or op.staging_batch_digest != challenge.staging_batch_digest
                or op.case_id != case
                or op.causal_group_id != group
                or op.host_identity != host.host_digest
                or op.database_before_digest
                != request_digest(before.model_dump(mode="json"))
                or op.database_after_digest
                != request_digest(after.model_dump(mode="json"))
                or op.fault_receipt_digest != receipt.evidence_digest
            ):
                raise ValueError("CausalScopeMismatch")
            if (
                op.target_step_ref not in fault.target_step_refs
                or not receipt.evidence.started_at.timestamp()
                <= op.at
                <= receipt.evidence.completed_at.timestamp()
            ):
                missing.add("ScenarioWitnessMissing")
                continue
            expected_target = (
                challenge.database_identity_digest
                if op.kind in {"database_unavailable", "database_recovered"}
                else op.action_ref
                if op.kind in {"dispatch", "unknown_dispatch"}
                else op.target_step_ref
            )
            if op.target != expected_target:
                failures.add("InvalidPersistentState")
            for field, records, owner in (
                ("action_ref", before.actions + after.actions, "step_ref"),
                ("model_call_ref", before.calls + after.calls, "step_ref"),
                ("attempt_ref", before.attempts + after.attempts, "step_ref"),
            ):
                ref = getattr(op, field)
                if ref is not None and not any(
                    r.ref == ref and getattr(r, owner) == op.target_step_ref
                    for r in records
                ):
                    missing.add("ScenarioWitnessMissing")
            if op.kind == "claim" and op.outcome == "accepted":
                step = steps[op.target_step_ref]
                if (
                    op.lease_digest != step.lease_digest
                    or op.lease_until != step.lease_until
                ):
                    failures.add("InvalidPersistentState")
                old = old_steps[op.target_step_ref]
                if (
                    old.lease_digest
                    and old.lease_digest != op.lease_digest
                    and op.at < (old.lease_until or 0)
                ):
                    failures.add("LeaseOverlap")
            ops.append(op)
    if len({o.host_identity for o in ops}) < 2 or not _scenario(
        case, before, after, ops, fault, receipt.evidence.started_at.timestamp()
    ):
        missing.add("ScenarioWitnessMissing")
    return failures, missing


def verify_causal_acceptance(challenge, trust, receipts, *, now=None):
    if challenge.evidence_version != 3 or any(
        r.evidence.evidence_version != 3 for r in receipts
    ):
        raise ValueError("EvidenceVersionMismatch")
    if (
        len(receipts) > 100
        or sum(len(r.model_dump_json().encode("utf-8")) for r in receipts) > 8_000_000
    ):
        raise ValueError("EvidenceBoundsExceeded")
    # Event identities exclude case/scope so rewrapping an event cannot make it
    # two independent exercises, even with a new signed witness digest.
    events, host_cases = set(), set()
    for receipt in receipts:
        if isinstance(receipt.evidence.payload, CausalHost):
            host_case = (
                receipt.evidence.payload.case_id,
                receipt.evidence.payload.host_digest,
            )
            if host_case in host_cases:
                raise ValueError("CausalEventReplay")
            host_cases.add(host_case)
            for op in receipt.evidence.payload.operations:
                event = request_digest(
                    (
                        op.host_identity,
                        op.kind,
                        op.target,
                        op.at,
                        op.lease_digest,
                        op.attempt_ref,
                    )
                )
                if event in events:
                    raise ValueError("CausalEventReplay")
                events.add(event)
    return _verify_acceptance(
        challenge,
        trust,
        receipts,
        now=now,
        signing=signing_bytes,
        witness=lambda *args: True,
        report_type=RCCausalAcceptance,
        causal_check=check_causality,
        transition_check=lambda before, after: transition_failures(
            before, after, allow_initialization=True
        ),
    )
