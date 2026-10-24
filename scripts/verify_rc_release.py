"""Mandatory offline RC evidence gate; historical v2/v3 entry points stay intact.

Exit 0: exact reviewed saved A-J evidence accepted, NOT production approval.
Exit 1: incomplete/unproven saved evidence. Exit 2: rejected evidence.
precheck is read-only; verify registers one final attempt in the existing local
challenge registry, including a completed FAIL/NOT_VERIFIED report.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from rc_acceptance import (
    AcceptanceTrust,
    CaseId,
    Digest,
    Frozen,
    consume_challenge,
    evidence_set_digest,
)
from rc_causal import CausalChallenge, CausalSignedEvidence, verify_causal_acceptance
from rc_host_witness import (
    ProvenanceBinding,
    check_registration,
    read_journal,
    unsigned_draft,
)
from rc_witness_provenance import precheck as provenance_precheck
from verify_rc_acceptance import (
    bounded_json,
    context,
    matches,
    read_prefix,
    reject_constant,
    unique_fields,
)

from agentflow.application.ports.model_request import canonical_json, request_digest

Status = Literal["PASS", "FAIL", "NOT_VERIFIED"]
CASES = "ABCDEFGHIJ"


class ReleaseCase(Frozen):
    case_id: CaseId
    status: Status = "NOT_VERIFIED"
    provenance_validation: Status = "NOT_VERIFIED"
    signed_evidence_validation: Status = "NOT_VERIFIED"
    independent_review: Status = "NOT_VERIFIED"
    causal_acceptance: Status = "NOT_VERIFIED"
    claim_invocations: Literal[
        "OVERLAPPING_INVOCATIONS",
        "SEQUENTIAL_VALID_LEASE",
        "TIMING_UNCERTAIN",
        "NO_CONTENTION_PROOF",
        "NOT_APPLICABLE",
    ] = "NOT_APPLICABLE"
    evidence_digest: Digest | None = None
    reviewed_evidence_digest: Digest | None = None
    binding_digest: Digest | None = None
    errors: tuple[str, ...] = ()


class ReleaseReport(Frozen):
    schema_version: Literal[1] = 1
    evidence_protocol: Literal[3] = 3
    status: Literal["FAIL", "NOT_VERIFIED"] = "NOT_VERIFIED"
    software_validation: Status = "NOT_VERIFIED"
    software_validation_scope: Literal["offline_input_and_verifier_checks"] = (
        "offline_input_and_verifier_checks"
    )
    saved_evidence_validation: Status = "NOT_VERIFIED"
    provenance_validation: Status = "NOT_VERIFIED"
    signed_evidence_validation: Status = "NOT_VERIFIED"
    independent_review: Status = "NOT_VERIFIED"
    transaction_contention: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    physical_hosts: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    cross_host_recovery: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    provider_acceptance: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    token_conformance: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    production_release: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    challenge_digest: Digest | None = None
    evidence_digest: Digest | None = None
    context_digest: Digest | None = None
    cases: tuple[ReleaseCase, ...]
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "SingleRegistryFileConsumptionOnly",
        "PhysicalHostAttestationNotVerified",
        "InvocationOverlapIsNotSQLLockProof",
        "LiveProviderAndTokenConformanceNotVerified",
        "ProductionAuthorizationRequired",
        "SoftwareTestResultsRequireSeparateCI",
    )


class CustodyReleaseReport(ReleaseReport):
    schema_version: Literal[2] = 2
    custody_validation: Literal["PASS", "FAIL"]
    manifest_digest: Digest | None = None
    bundle_content_digest: Digest | None = None
    prepared_manifest_digest: Digest | None = None
    custody_limitations: tuple[str, ...] = (
        "ProtectedStoreOwnerAndAdministratorTrusted",
        "SingleProtectedStoreAndOriginalRegistryRequired",
        "WindowsDirectoryPowerLossDurabilityNotVerified",
        "SyntheticSignaturesDoNotAuthenticatePhysicalSources",
    )


def aggregate(statuses):
    values = set(statuses)
    return (
        "FAIL" if "FAIL" in values else "PASS" if values == {"PASS"} else "NOT_VERIFIED"
    )


def regular(path, *, directory=False):
    path = Path(path)
    if (
        path.is_symlink()
        or path.is_junction()
        or not (path.is_dir() if directory else path.is_file())
    ):
        raise ValueError("ReleaseFileTypeRejected")
    return path


def directory(path, allowed):
    root = regular(path, directory=True)
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= len(allowed) or entry.name not in allowed or entry.is_symlink():
                raise ValueError("ReleaseBundleRejected")
    return root


def read_json(path, maximum):
    return bounded_json(regular(path), maximum)


def load_bundle(root, workers):
    root = directory(root, {"build.json", "infrastructure.json", *CASES})
    receipts, inputs, size = [], {}, 0

    def receipt(path, source, case=None, phase=None):
        nonlocal size
        if not path.exists():
            return None
        value = read_json(path, min(2_100_000, 8_000_000 - size))
        result = CausalSignedEvidence.model_validate(value)
        size += len(result.model_dump_json().encode("utf-8"))
        payload = result.evidence.payload
        if (
            size > 8_000_000
            or len(receipts) >= 100
            or payload.source != source
            or getattr(payload, "case_id", None) != case
            or (phase is not None and payload.phase != phase)
        ):
            raise ValueError("ReleaseSourceRejected")
        receipts.append(result)
        return result

    for source in ("build", "infrastructure"):
        receipt(root / f"{source}.json", source)
    for case in CASES:
        folder = root / case
        binding, roots, hosts = None, [], []
        if folder.exists():
            allowed = {
                "before.json",
                "after.json",
                "fault.json",
                "binding.json",
                "reviewer.json",
            } | {
                name for i in range(workers) for name in (f"host-{i}", f"host-{i}.json")
            }
            directory(folder, allowed)
            for phase in ("before", "after"):
                receipt(folder / f"{phase}.json", "database", case, phase)
            receipt(folder / "fault.json", "fault", case)
            receipt(folder / "reviewer.json", "reviewer", case)
            if (folder / "binding.json").exists():
                binding = ProvenanceBinding.model_validate(
                    read_json(folder / "binding.json", 8000)
                )
                if binding.case_id != case:
                    raise ValueError("ReleaseCaseMismatch")
            for i in range(workers):
                hosts.append(receipt(folder / f"host-{i}.json", "host", case))
                journal = folder / f"host-{i}"
                if journal.exists():
                    regular(journal, directory=True)
                roots.append(journal)
        inputs[case] = binding, roots, hosts
    return receipts, inputs


def reference_errors(host, sources):
    """Check every signed reference; an unrelated good event cannot mask it."""
    snapshots = [
        r.evidence.payload.snapshot
        for r in sources
        if r.evidence.payload.source == "database"
    ]
    records = {
        name: {
            item.ref: item for snapshot in snapshots for item in getattr(snapshot, name)
        }
        for name in (
            "steps",
            "actions",
            "calls",
            "attempts",
            "permits",
            "providers",
            "approvals",
            "reconciliations",
        )
    }
    errors = set()
    fault = next(
        r.evidence.payload for r in sources if r.evidence.payload.source == "fault"
    )
    for op in host.operations:
        if op.target_step_ref not in records["steps"]:
            errors.add("OperationReferenceMismatch")
        for field, table in (
            ("action_ref", "actions"),
            ("model_call_ref", "calls"),
            ("attempt_ref", "attempts"),
            ("provider_ref", "providers"),
            ("permit_ref", "permits"),
            ("approval_ref", "approvals"),
            ("reconciliation_ref", "reconciliations"),
        ):
            ref = getattr(op, field)
            row = records[table].get(ref)
            if ref is not None and (
                row is None
                or (hasattr(row, "step_ref") and row.step_ref != op.target_step_ref)
            ):
                errors.add("OperationReferenceMismatch")
        for field, table in (
            ("approval_ref", "approvals"),
            ("reconciliation_ref", "reconciliations"),
        ):
            row = records[table].get(getattr(op, field))
            if row and (
                row.action_ref != op.action_ref
                or (
                    field == "approval_ref"
                    and (row.decision != "approved" or row.consumed_at is None)
                )
                or (field == "reconciliation_ref" and not row.applied)
            ):
                errors.add("OperationReferenceMismatch")
        attempt = records["attempts"].get(op.attempt_ref)
        permit = records["permits"].get(op.permit_ref)
        if (
            attempt
            and (
                attempt.call_ref != op.model_call_ref
                or (
                    attempt.provider_ref is not None
                    and attempt.provider_ref != op.provider_ref
                )
            )
            or permit
            and (
                permit.attempt_ref != op.attempt_ref
                or permit.provider_ref != op.provider_ref
            )
        ):
            errors.add("OperationReferenceMismatch")
        if op.kind in {"stale_commit", "cancelled_commit"} and (
            op.outcome != "rejected"
            or op.error_code not in {"LostLease", "StateConflict"}
        ):
            errors.add("RejectionEvidenceMismatch")
        if (
            host.case_id == "F"
            and op.kind == "model_reserve"
            and op.provider_ref != fault.provider_ref
        ):
            errors.add("OperationReferenceMismatch")
    return errors


def case_check(case, inputs, receipts, challenge, registry, trust, collector, workers):
    binding, roots, hosts = inputs
    selected = [
        r for r in receipts if getattr(r.evidence.payload, "case_id", case) == case
    ]
    sources = [
        r for r in selected if r.evidence.payload.source not in {"host", "reviewer"}
    ]
    reviewed = [r for r in selected if r.evidence.payload.source != "reviewer"]
    reviews = [r for r in selected if r.evidence.payload.source == "reviewer"]
    review, errors = "NOT_VERIFIED", set()
    if len(reviews) == 1 and reviews[0].evidence.payload.review_status == "rejected":
        review = "FAIL"
        errors.add("IndependentReviewRejected")
    elif (
        len(reviews) == 1
        and reviews[0].evidence.payload.reviewed_evidence_digest
        == evidence_set_digest(reviewed)
        and reviewed
        and reviews[0].evidence.started_at
        >= max(r.evidence.completed_at for r in reviewed)
    ):
        review = "PASS"
    else:
        errors.add("ExactEvidenceReviewMissing")
    provenance, signed, invocation = (
        "NOT_VERIFIED",
        "NOT_VERIFIED",
        "NO_CONTENTION_PROOF" if case == "A" else "NOT_APPLICABLE",
    )
    source_complete = len(sources) == 5 and {
        r.evidence.payload.source for r in sources
    } == {"database", "fault", "build", "infrastructure"}
    journals_complete = len(roots) == len(workers) and all(
        (p / "header.json").is_file() and (p / "closed.json").is_file() for p in roots
    )
    if binding is None or not source_complete or not journals_complete:
        errors.add("CompleteProvenanceMissing")
    else:
        checked = provenance_precheck(
            challenge=challenge,
            registry=registry,
            roots=roots,
            binding=binding,
            trust=trust,
            receipts=sources,
            collector_digest=collector,
        )
        invocation = checked["case_a"]
        provenance = (
            "PASS"
            if checked["eligible_for_signing"]
            else "NOT_VERIFIED"
            if set(checked["errors"])
            <= {"ConcurrentInvocationProofMissing", "WitnessClockUnverified"}
            else "FAIL"
        )
        errors.update(checked["errors"])
        if invocation == "SEQUENTIAL_VALID_LEASE":
            errors.add("SequentialClaimIsNotContentionProof")
        if invocation == "TIMING_UNCERTAIN":
            errors.add("ContentionTimingUncertain")
        matched = True
        for index, root in enumerate(roots):
            try:
                header, _, _ = read_journal(root, challenge, registry)
                if header["host_identity"] != workers[index]:
                    raise ValueError("ReleaseHostMismatch")
                draft = unsigned_draft(
                    root, challenge, registry, trust, sources, binding
                )
                host = hosts[index]
                if host is None:
                    matched = False
                    errors.add("SignedHostReceiptMissing")
                elif (
                    host.evidence.payload.model_dump(mode="json") != draft["payload"]
                    or host.evidence.started_at.isoformat() != draft["started_at"]
                    or host.evidence.completed_at.isoformat() != draft["completed_at"]
                ):
                    matched = False
                    signed = "FAIL"
                    errors.add("HostJournalMismatch")
                if host is not None:
                    bad = reference_errors(host.evidence.payload, sources)
                    if bad:
                        provenance = "FAIL"
                        errors.update(bad)
            except Exception:
                matched = False
                errors.add("JournalDerivationRejected")
                provenance = "FAIL" if provenance != "NOT_VERIFIED" else provenance
        if matched:
            signed = "PASS"
    return ReleaseCase(
        case_id=case,
        provenance_validation=provenance,
        signed_evidence_validation=signed,
        independent_review=review,
        claim_invocations=invocation,
        evidence_digest=evidence_set_digest(selected),
        reviewed_evidence_digest=evidence_set_digest(reviewed),
        binding_digest=request_digest(binding.model_dump(mode="json"))
        if binding
        else None,
        errors=tuple(sorted(errors)),
    )


def evaluate(args):
    # No precheck report is an input. Both commands read and recompute the same
    # bounded source bundle, with independent trust and collector pins.
    for path in (args.config, args.v1_report, args.plan, args.challenge, args.trust):
        regular(path)
    regular(args.state_dir, directory=True)
    regular(Path(args.state_dir) / "issued.json")
    config, original, plan, batch = context(args)
    challenge = CausalChallenge.model_validate(read_json(args.challenge, 16000))
    matches(challenge, config, original, batch)
    check_registration(args.state_dir, challenge)
    if challenge.alembic_revision != "0008" or not re.fullmatch(
        r"[a-f0-9]{64}", args.collector_sha256
    ):
        raise ValueError("ReleaseContextRejected")
    raw = read_prefix(regular(args.trust), 32001)
    if len(raw) > 32000 or hashlib.sha256(raw).hexdigest() != args.trust_sha256:
        raise ValueError("TrustPinMismatch")
    trust = AcceptanceTrust.model_validate(
        json.loads(raw, object_pairs_hook=unique_fields, parse_constant=reject_constant)
    )
    workers = [h.host_digest for h in config.hosts if h.role == "worker"]
    receipts, inputs = load_bundle(args.bundle, len(workers))
    expected_runs = {
        request_digest((str(challenge.environment_id), batch, ("run", r["id"])))
        for r in plan["runs"]
    }
    for receipt in receipts:
        payload = receipt.evidence.payload
        if (
            (payload.source == "build" and payload.identity != config.expected_identity)
            or (payload.source == "host" and payload.host_digest not in workers)
            or (
                payload.source == "infrastructure"
                and {h for h, _ in payload.host_failure_domains} != set(workers)
            )
            or (
                payload.source == "database"
                and any(s.run_ref not in expected_runs for s in payload.snapshot.steps)
            )
        ):
            raise ValueError("ReleaseContextMismatch")
    cases = [
        case_check(
            case,
            inputs[case],
            receipts,
            challenge,
            args.state_dir,
            trust,
            args.collector_sha256,
            workers,
        )
        for case in CASES
    ]
    errors = ()
    try:
        causal = verify_causal_acceptance(challenge, trust, receipts)
    except Exception:
        errors = ("SignedOrCausalEvidenceRejected",)
        cases = [
            c.model_copy(
                update={
                    "status": "FAIL",
                    "signed_evidence_validation": "FAIL",
                    "independent_review": "NOT_VERIFIED",
                    "errors": tuple(
                        sorted({*c.errors, "SignedOrCausalEvidenceRejected"})
                    ),
                }
            )
            for c in cases
        ]
    else:
        final = []
        for case, old in zip(cases, causal.cases, strict=True):
            review = case.independent_review
            if "IndependentReviewMissing" in old.limitations:
                review = "NOT_VERIFIED"
            status = aggregate(
                (
                    case.provenance_validation,
                    case.signed_evidence_validation,
                    review,
                    old.status,
                )
            )
            final.append(
                case.model_copy(
                    update={
                        "status": status,
                        "causal_acceptance": old.status,
                        "independent_review": review,
                        "errors": tuple(
                            sorted(
                                {
                                    *case.errors,
                                    *old.observed_invariants,
                                    *old.limitations,
                                }
                            )
                        ),
                    }
                )
            )
        cases = final
    check_registration(args.state_dir, challenge)
    saved = aggregate(c.status for c in cases)
    return ReleaseReport(
        status="FAIL" if saved == "FAIL" else "NOT_VERIFIED",
        software_validation="PASS",
        saved_evidence_validation=saved,
        provenance_validation=aggregate(c.provenance_validation for c in cases),
        signed_evidence_validation=aggregate(
            c.signed_evidence_validation for c in cases
        ),
        independent_review=aggregate(c.independent_review for c in cases),
        challenge_digest=request_digest(challenge.model_dump(mode="json")),
        evidence_digest=evidence_set_digest(receipts),
        context_digest=request_digest(
            (
                config.model_dump(mode="json"),
                original.model_dump(mode="json"),
                plan,
                args.trust_sha256,
                args.collector_sha256,
            )
        ),
        cases=cases,
        errors=errors,
    )


def run(args):
    if args.command in {"custody-precheck", "custody-verify"}:
        from rc_evidence_custody import run_release

        return run_release(args)
    try:
        if args.command not in {"precheck", "verify"}:
            raise ValueError("ReleaseCommandRejected")
        report = evaluate(args)
        if args.command == "verify" and not report.errors:
            challenge = CausalChallenge.model_validate(read_json(args.challenge, 16000))
            check_registration(args.state_dir, challenge)
            if (
                request_digest(challenge.model_dump(mode="json"))
                != report.challenge_digest
            ):
                raise ValueError("ChallengeRegistrationMismatch")
            consume_challenge(args.state_dir, challenge)
        return (
            0
            if report.saved_evidence_validation == "PASS"
            else 2
            if report.saved_evidence_validation == "FAIL"
            else 1
        ), report
    except Exception as error:
        # No untrusted values, file paths, prompts, tokens or exception text.
        safe = {
            "TrustPinMismatch",
            "ChallengeReplay",
            "EvidenceExpired",
            "ChallengeRegistrationMismatch",
            "ReleaseFileTypeRejected",
            "ReleaseBundleRejected",
            "ReleaseSourceRejected",
            "ReleaseCaseMismatch",
            "ReleaseContextMismatch",
            "ReleaseContextRejected",
            "AcceptanceContextMismatch",
            "EvidenceBoundsExceeded",
            "DuplicateEvidenceField",
        }
        code = (
            error.args[0]
            if len(error.args) == 1 and isinstance(error.args[0], str)
            else None
        )
        return 2, ReleaseReport(
            status="FAIL",
            software_validation="FAIL",
            saved_evidence_validation="FAIL",
            cases=[ReleaseCase(case_id=c) for c in CASES],
            errors=(code if code in safe else "ReleaseVerificationRejected",),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("precheck", "verify", "custody-precheck", "custody-verify")
    )
    parser.add_argument("--custody-store")
    parser.add_argument("--manifest-sha256")
    for name in (
        "config",
        "v1-report",
        "plan",
        "challenge",
        "state-dir",
        "trust",
        "trust-sha256",
        "collector-sha256",
        "bundle",
    ):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    needed = (
        (
            "custody_store",
            "manifest_sha256",
            "state_dir",
            "trust_sha256",
            "collector_sha256",
        )
        if args.command.startswith("custody-")
        else (
            "config",
            "v1_report",
            "plan",
            "challenge",
            "state_dir",
            "trust",
            "trust_sha256",
            "collector_sha256",
            "bundle",
        )
    )
    if any(getattr(args, name) is None for name in needed):
        parser.error("required evidence inputs missing")
    code, report = run(args)
    value = report.model_dump(mode="json")
    print(canonical_json({**value, "report_digest": request_digest(value)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
