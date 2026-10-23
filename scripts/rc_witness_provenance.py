"""Offline unsigned precheck of complete, independently fault-bound journals.

This does not consume challenges, sign evidence, authenticate physical hosts or
upgrade the v3 acceptance protocol's temporal proximity into SQL contention.
"""

import argparse
import hashlib
import json
from pathlib import Path

from rc_acceptance import AcceptanceTrust, evidence_set_digest
from rc_causal import CausalChallenge, CausalSignedEvidence, verify_causal_acceptance
from rc_host_witness import (
    ProvenanceBinding,
    check_registration,
    read_journal,
    unsigned_draft,
)
from verify_rc_acceptance import bounded_json, reject_constant, unique_fields

from agentflow.application.ports.model_request import canonical_json, request_digest


def case_a_contention(journals):
    """Conservative intersection of invocation intervals, never a claim bool."""
    candidates = [
        (h["host_identity"], o)
        for h, observations, _ in journals
        for o in observations
        if o.source == "store.claim"
        and o.operation.kind == "claim"
        and o.operation.error_code is None
        and o.invocation is not None
        and o.invocation.clock_source == "postgresql"
    ]
    sequential, uncertain = False, False
    for host, win in candidates:
        if win.operation.outcome != "accepted" or win.invocation.outcome != "task":
            continue
        for other, lose in candidates:
            if (
                host == other
                or lose.operation.outcome != "rejected"
                or lose.invocation.outcome != "none"
                or win.run_ref is None
                or lose.run_ref != win.run_ref
                or lose.target_step_ref != win.target_step_ref
                or win.lease_digest is None
                or lose.lease_digest != win.lease_digest
                or lose.operation.lease_until != win.operation.lease_until
            ):
                continue
            a, b = win.invocation, lose.invocation
            if max(
                a.started_at + a.clock_error_seconds,
                b.started_at + b.clock_error_seconds,
            ) < min(
                a.completed_at - a.clock_error_seconds,
                b.completed_at - b.clock_error_seconds,
            ):
                return "OVERLAPPING_INVOCATIONS"
            if (
                a.completed_at + a.clock_error_seconds
                <= b.started_at - b.clock_error_seconds
            ):
                sequential = True
            else:
                uncertain = True
    if uncertain:
        return "TIMING_UNCERTAIN"
    if sequential:
        return "SEQUENTIAL_VALID_LEASE"
    return "NO_CONTENTION_PROOF"


def _check(challenge, registry, roots, binding, trust, receipts, collector_digest):
    check_registration(registry, challenge)
    if not 2 <= len(roots) <= 3:
        raise ValueError("TwoHostJournalMissing")
    # Reuse all existing signature, trust, challenge, schema and DB checks.
    verified = verify_causal_acceptance(challenge, trust, receipts)
    result = next(c for c in verified.cases if c.case_id == binding.case_id)
    if result.observed_invariants:
        raise ValueError("WitnessDatabaseConflict")
    sources = [
        r
        for r in receipts
        if getattr(r.evidence.payload, "case_id", None) == binding.case_id
    ]
    databases = [
        r.evidence.payload for r in sources if r.evidence.payload.source == "database"
    ]
    before = [p.snapshot for p in databases if p.phase == "before"]
    after = [p.snapshot for p in databases if p.phase == "after"]
    faults = [r for r in sources if r.evidence.payload.source == "fault"]
    if len(before) != 1 or len(after) != 1 or len(faults) != 1:
        raise ValueError("SignedSourceEvidenceMissing")
    before, after, fault = before[0], after[0], faults[0]
    if (
        before.database_backend != "postgresql"
        or after.database_backend != "postgresql"
        or not before.observed_at
        <= fault.evidence.started_at
        <= fault.evidence.completed_at
        <= after.observed_at
        or fault.evidence.payload.protected_record_digest
        != request_digest(binding.model_dump(mode="json"))
    ):
        raise ValueError("WitnessProvenanceMismatch")
    journals = [read_journal(root, challenge, registry) for root in roots]
    identities = [(h["host_identity"], digest) for h, _, digest in journals]
    headers = [request_digest(h) for h, _, _ in journals]
    trusted_hosts = {s.host_digest for s in trust.signers if s.role == "host"}
    if (
        len(set(h for h, _ in identities)) != len(journals)
        or len(set(d for _, d in identities)) != len(journals)
        or len(set(binding.journals)) != len(journals)
        or set(identities) != set(binding.journals)
        or len(binding.process_headers) != len(journals)
        or set(headers) != set(binding.process_headers)
        or len({h["process_nonce"] for h, _, _ in journals}) != len(journals)
    ):
        raise ValueError("WitnessProcessBindingMissing")
    steps = {s.ref: s for s in before.steps + after.steps}
    seen, winners, in_window = set(), set(), []
    for root, (header, observations, _) in zip(roots, journals, strict=True):
        selected = []
        if (
            header["host_identity"] not in trusted_hosts
            or header["case_id"] != binding.case_id
            or header["collector_digest"] != collector_digest
            or header["deployment_sha"] != challenge.deployment_sha
        ):
            raise ValueError("WitnessProvenanceMismatch")
        # Also validates the full fault binding and before/after reference group;
        # the returned draft stays in memory, never signs or writes to a journal.
        unsigned_draft(root, challenge, registry, trust, receipts, binding)
        for observed in observations:
            step = steps.get(observed.target_step_ref)
            if step is None:
                raise ValueError("WitnessTargetMissing")
            invocation = observed.invocation
            if invocation is None:
                continue  # Legacy observations cannot prove invocation overlap.
            if (
                invocation.outcome != "database_error"
                and observed.run_ref != step.run_ref
            ) or invocation.invocation_id in seen:
                raise ValueError("WitnessInvocationReplay")
            seen.add(invocation.invocation_id)
            low = invocation.started_at - invocation.clock_error_seconds
            high = invocation.completed_at + invocation.clock_error_seconds
            # Prelude outside the fault interval stays in the complete journal.
            if (
                high < fault.evidence.started_at.timestamp()
                or low > fault.evidence.completed_at.timestamp()
            ):
                continue
            if (
                not fault.evidence.started_at.timestamp()
                <= low
                <= high
                <= fault.evidence.completed_at.timestamp()
            ):
                raise ValueError("WitnessClockUnverified")
            if invocation.clock_source != "postgresql":
                raise ValueError("PostgresEvidenceMissing")
            op = observed.operation
            if (
                (
                    invocation.outcome == "task"
                    and (
                        observed.source != "store.claim"
                        or op.outcome != "accepted"
                        or not low <= op.at <= high
                    )
                )
                or (
                    invocation.outcome == "none"
                    and (
                        op.outcome not in {"rejected", "unavailable"}
                        or op.at
                        < invocation.completed_at - invocation.clock_error_seconds
                    )
                )
                or (
                    invocation.outcome == "database_error"
                    and (
                        observed.source != "database.error"
                        or op.error_code != "DatabaseUnavailable"
                    )
                )
            ):
                raise ValueError("WitnessInvocationMismatch")
            if observed.operation.kind == "claim" and observed.operation.outcome in {
                "accepted",
                "rejected",
            }:
                if (
                    observed.lease_digest != step.lease_digest
                    or observed.operation.lease_until != step.lease_until
                    or observed.operation.error_code is not None
                ):
                    raise ValueError("WitnessCommitUnconfirmed")
                if binding.case_id == "A" and observed.operation.outcome == "accepted":
                    prior = next((s for s in before.steps if s.ref == step.ref), None)
                    if (
                        prior is None
                        or prior.status != "ready"
                        or step.status != "running"
                        or step.ref in winners
                    ):
                        raise ValueError("WitnessCommitUnconfirmed")
                    winners.add(step.ref)
            selected.append(observed)
        in_window.append((header, selected, ""))
    contention = (
        case_a_contention(in_window) if binding.case_id == "A" else "NOT_APPLICABLE"
    )
    if binding.case_id == "A" and contention != "OVERLAPPING_INVOCATIONS":
        return contention, ["ConcurrentInvocationProofMissing"]
    return contention, []


def precheck(*, challenge, registry, roots, binding, trust, receipts, collector_digest):
    contention, errors = "NO_CONTENTION_PROOF", []
    try:
        contention, errors = _check(
            challenge, registry, roots, binding, trust, receipts, collector_digest
        )
    except Exception as error:
        # Never echo paths, tokens, driver exceptions or arbitrary imported text.
        safe = {
            "ChallengeReplay",
            "EvidenceExpired",
            "EvidenceScopeMismatch",
            "EvidenceSignatureRejected",
            "WitnessDatabaseConflict",
            "WitnessProvenanceMismatch",
            "WitnessProcessBindingMissing",
            "WitnessClockUnverified",
            "WitnessTargetMissing",
            "WitnessInvocationReplay",
            "WitnessInvocationMismatch",
            "WitnessCommitUnconfirmed",
            "TwoHostJournalMissing",
            "SignedSourceEvidenceMissing",
            "PostgresEvidenceMissing",
            "WitnessIncomplete",
            "ChallengeRegistrationMismatch",
        }
        code = (
            error.args[0]
            if len(error.args) == 1 and isinstance(error.args[0], str)
            else None
        )
        errors = [code if code in safe else "WitnessProvenanceRejected"]
    report = dict(
        version=1,
        status="NOT_VERIFIED",
        unsigned=True,
        eligible_for_signing=not errors,
        case_id=binding.case_id,
        challenge_digest=request_digest(challenge.model_dump(mode="json")),
        binding_digest=request_digest(binding.model_dump(mode="json")),
        source_evidence_digest=evidence_set_digest(receipts),
        case_a=contention,
        transaction_contention="NOT_VERIFIED",
        physical_hosts="NOT_VERIFIED",
        errors=errors,
        limitations=[
            "ProtectedProcessObservationRequired",
            "IndependentReviewerRequired",
            "V3DoesNotSignInvocationIntervals",
        ],
    )
    return {**report, "report_digest": request_digest(report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "challenge",
        "state-dir",
        "binding",
        "trust",
        "trust-sha256",
        "collector-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--journal", action="append", required=True)
    args = parser.parse_args()
    try:
        with Path(args.trust).open("rb") as stream:
            data = stream.read(32001)
        if len(data) > 32000 or hashlib.sha256(data).hexdigest() != args.trust_sha256:
            raise ValueError("TrustPinMismatch")
        trust = AcceptanceTrust.model_validate(
            json.loads(
                data, object_pairs_hook=unique_fields, parse_constant=reject_constant
            )
        )
        challenge = CausalChallenge.model_validate(bounded_json(args.challenge, 16000))
        binding = ProvenanceBinding.model_validate(bounded_json(args.binding, 8000))
        receipts = [
            CausalSignedEvidence.model_validate(
                bounded_json(Path(args.binding).parent / name, 2_100_000)
            )
            for name in ("before.json", "after.json", "fault.json")
        ]
        result = precheck(
            challenge=challenge,
            registry=args.state_dir,
            roots=args.journal,
            binding=binding,
            trust=trust,
            receipts=receipts,
            collector_digest=args.collector_sha256,
        )
        print(canonical_json(result))
        return 1 if result["eligible_for_signing"] else 2
    except Exception:
        print(
            canonical_json(
                dict(
                    status="NOT_VERIFIED",
                    unsigned=True,
                    eligible_for_signing=False,
                    errors=["WitnessProvenanceRejected"],
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
