"""Independent RC acceptance: offline verification; opt-in staging READ ONLY capture.

Exit 0: reviewed saved A-J evidence accepted (not production authorization).
Exit 1: incomplete evidence, NOT_VERIFIED. Exit 2: rejected/failed evidence.
The only writes are private local challenge registration/one-use tombstones.
"""

import argparse
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rc_acceptance import (
    AcceptanceChallenge,
    AcceptanceTrust,
    BuildEvidence,
    DatabaseEvidence,
    HostEvidence,
    InfrastructureEvidence,
    SignedEvidence,
    consume_challenge,
    issue_challenge,
    verify_acceptance,
)
from rc_acceptance_db import checked_plan, collect_snapshot, database_identity
from rc_staging import RCStagingEvidenceReport, StagingConfiguration, required_revision

from agentflow.application.ports.model_request import request_digest


def bounded_json(path, maximum):
    # read(max+1) bounds the actual read, including files growing after stat.
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("EvidenceBoundsExceeded")
    return json.loads(
        raw, object_pairs_hook=unique_fields, parse_constant=reject_constant
    )


def unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("DuplicateEvidenceField")
        result[name] = value
    return result


def reject_constant(value):
    raise ValueError("InvalidEvidenceNumber")


def context(args):
    config = StagingConfiguration.model_validate(bounded_json(args.config, 32000))
    original = RCStagingEvidenceReport.model_validate(
        bounded_json(args.v1_report, 32000)
    )
    plan = bounded_json(args.plan, 8000)
    batch = checked_plan(plan, config.environment_id)
    if (
        original.test_environment_id != config.environment_id
        or original.git_commit_sha != config.expected_identity.git_commit_sha
        or original.application_version != config.expected_identity.application_version
        or original.database_schema_revision != required_revision()
        or original.preflight.status != "PASS"
    ):
        raise ValueError("PreflightEvidenceRequired")
    return config, original, plan, batch


def matches(challenge, config, original, batch):
    if (
        challenge.environment_id != config.environment_id
        or challenge.deployment_sha != config.expected_identity.git_commit_sha
        or challenge.application_version != config.expected_identity.application_version
        or challenge.alembic_revision != original.database_schema_revision
        or challenge.v1_report_digest
        != request_digest(original.model_dump(mode="json"))
        or challenge.staging_batch_digest != batch
    ):
        raise ValueError("AcceptanceContextMismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("issue", "snapshot", "verify"))
    parser.add_argument("--config")
    parser.add_argument("--v1-report")
    parser.add_argument("--plan")
    parser.add_argument(
        "--state-dir", help="private, verifier-controlled single challenge directory"
    )
    parser.add_argument("--challenge")
    parser.add_argument("--database-identity-digest")
    parser.add_argument("--trust")
    parser.add_argument(
        "--trust-sha256",
        help="out-of-band approved SHA256 of the exact public trust file",
    )
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--case", choices=tuple("ABCDEFGHIJ"))
    parser.add_argument("--phase", choices=("before", "after"))
    parser.add_argument("--read-staging", action="store_true")
    args = parser.parse_args()
    try:
        config, original, plan, batch = context(args)
        if args.command == "issue":
            now = datetime.now(timezone.utc)
            if (
                not original.completed_at
                <= now
                <= original.completed_at + timedelta(hours=1)
            ):
                raise ValueError("PreflightExpired")
            challenge = AcceptanceChallenge(
                environment_id=config.environment_id,
                deployment_sha=original.git_commit_sha,
                application_version=original.application_version,
                alembic_revision=original.database_schema_revision,
                database_identity_digest=args.database_identity_digest,
                staging_batch_digest=batch,
                v1_report_digest=request_digest(original.model_dump(mode="json")),
                execution_nonce=secrets.token_hex(32),
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
            issue_challenge(args.state_dir, challenge)
            print(challenge.model_dump_json())
            return 0
        challenge = AcceptanceChallenge.model_validate(
            bounded_json(args.challenge, 16000)
        )
        matches(challenge, config, original, batch)
        if args.command == "snapshot":
            if not args.read_staging or not args.case or not args.phase:
                raise ValueError("StagingReadOptInRequired")
            from rc_staging import (
                connect,
                deployment_identity,
                evaluate,
                read_database_facts,
            )

            engine = connect(os.environ["AGENTFLOW_RC_DATABASE_URL"], "audit")
            try:
                if database_identity(
                    config, engine.url
                ) != challenge.database_identity_digest or evaluate(
                    config, read_database_facts(engine, config), deployment_identity()
                ):
                    raise ValueError("StagingPreflightRejected")
                now = datetime.now(timezone.utc)
                if not challenge.issued_at <= now <= challenge.expires_at:
                    raise ValueError("EvidenceExpired")
                snapshot = collect_snapshot(engine, plan, config.environment_id)
                # Unsigned collector result; external protected signer must bind
                # this payload and its actual time range to the challenge.
                print(
                    json.dumps(
                        {
                            "challenge": challenge.model_dump(mode="json"),
                            "started_at": now.isoformat(),
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                            "payload": DatabaseEvidence(
                                case_id=args.case,
                                phase=args.phase,
                                snapshot=snapshot.model_dump(mode="json"),
                            ).model_dump(mode="json"),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            finally:
                engine.dispose()
        if args.read_staging:
            raise ValueError("OfflineVerificationOnly")
        # Trust is an independent verifier input, never imported from evidence.
        with Path(args.trust).open("rb") as stream:
            raw = stream.read(32001)
        if len(raw) > 32000 or hashlib.sha256(raw).hexdigest() != args.trust_sha256:
            raise ValueError("TrustPinMismatch")
        trust = AcceptanceTrust.model_validate(
            json.loads(
                raw, object_pairs_hook=unique_fields, parse_constant=reject_constant
            )
        )
        if len(args.evidence) > 100:
            raise ValueError("EvidenceBoundsExceeded")
        receipts, size = [], 0
        for path in args.evidence:
            value = bounded_json(path, min(2_100_000, 8_000_000 - size))
            receipt = SignedEvidence.model_validate(value)
            size += len(receipt.model_dump_json().encode("utf-8"))
            if size > 8_000_000:
                raise ValueError("EvidenceBoundsExceeded")
            receipts.append(receipt)
        # Build receipt is signed by an independent deployment authority; compare
        # every configuration digest, not merely the commit's printable name.
        if any(
            isinstance(r.evidence.payload, BuildEvidence)
            and r.evidence.payload.identity != config.expected_identity
            for r in receipts
        ):
            raise ValueError("BuildEvidenceMismatch")
        workers = {h.host_digest for h in config.hosts if h.role == "worker"}
        for receipt in receipts:
            payload = receipt.evidence.payload
            if isinstance(payload, HostEvidence) and payload.host_digest not in workers:
                raise ValueError("HostEvidenceMismatch")
            if (
                isinstance(payload, InfrastructureEvidence)
                and {host for host, _ in payload.host_failure_domains} != workers
            ):
                raise ValueError("HostEvidenceMismatch")
        report = verify_acceptance(challenge, trust, receipts)
        consume_challenge(args.state_dir, challenge)
        print(report.model_dump_json())
        statuses = {c.status for c in report.cases}
        return 2 if "FAIL" in statuses else (1 if "NOT_VERIFIED" in statuses else 0)
    except Exception:
        # Validation/driver/crypto exceptions may contain untrusted documents.
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "FAIL",
                    "error": "AcceptanceVerificationRejected",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
