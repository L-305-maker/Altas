"""Bounded SELECT-only upgrade check for 0006/0007/0008. No identities or content.

Exit 2: operator review required; exit 1: audit unavailable, never a healthy result.
Read AGENTFLOW_DATABASE_URL from a protected environment, preferably SELECT-only.
"""

import argparse
import json
import os
import time
from collections import Counter

from sqlalchemy import MetaData, Table, and_, create_engine, func, literal, select, text
from sqlalchemy.exc import SQLAlchemyError

from agentflow.application.ports.context_projection import ContextProjectionManifest
from agentflow.application.ports.evidence_checkpoint import EvidenceCheckpointPolicy
from agentflow.application.ports.model_request import (
    ModelRequestContract,
    ModelRequestError,
)
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.models.deepseek import model_lifecycle


def audit(engine, *, limit=1000, offset=0, timeout_seconds=30):
    if (
        type(limit) is not int
        or not 1 <= limit <= 10000
        or type(offset) is not int
        or not 0 <= offset <= 1_000_000
        or not 1 <= timeout_seconds <= 120
    ):
        raise ValueError("invalid audit bounds")
    counts, findings = Counter(), Counter()
    deadline = time.monotonic() + timeout_seconds
    with engine.connect() as db:
        try:
            if engine.dialect.name == "postgresql":
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
            elif engine.dialect.name == "sqlite":
                db.execute(text("PRAGMA query_only=ON"))
                db.execute(text("BEGIN"))
            else:
                raise ValueError("unsupported audit database")
            metadata = MetaData()
            sessions, steps, runs, calls, attempts = (
                Table(name, metadata, autoload_with=db)
                for name in (
                    "agent_sessions",
                    "steps",
                    "runs",
                    "agent_model_calls",
                    "agent_model_attempts",
                )
            )
            projection = attempts.c.get("context_projection", literal(None))
            contract = attempts.c.get("request_contract", literal(None))
            latest = (
                select(calls.c.id)
                .where(calls.c.session_id == sessions.c.id)
                .order_by(calls.c.round_index.desc())
                .limit(1)
                .correlate(sessions)
                .scalar_subquery()
            )
            call_count = (
                select(func.count())
                .where(calls.c.session_id == sessions.c.id)
                .correlate(sessions)
                .scalar_subquery()
            )
            rows = db.execute(
                select(
                    sessions.c.snapshot,
                    runs.c.spec.label("run_spec"),
                    steps.c.key.label("step_key"),
                    steps.c.status.label("step_status"),
                    runs.c.status.label("run_status"),
                    calls.c.status.label("call_status"),
                    call_count.label("call_count"),
                    projection.label("projection"),
                    contract.label("contract"),
                )
                .select_from(
                    sessions.join(steps, sessions.c.step_row_id == steps.c.id)
                    .join(runs, steps.c.run_id == runs.c.id)
                    .outerjoin(calls, calls.c.id == latest)
                    .outerjoin(
                        attempts,
                        and_(
                            attempts.c.session_id == sessions.c.id,
                            attempts.c.model_call_id == calls.c.model_call_id,
                            attempts.c.attempt_index == calls.c.attempts,
                        ),
                    )
                )
                .order_by(sessions.c.id)
                .offset(offset)
                .limit(limit + 1)
            ).mappings()
            truncated = False
            for index, row in enumerate(rows):
                if time.monotonic() >= deadline:
                    raise TimeoutError("ModelRetryUpgradeAuditDeadline")
                if index == limit:
                    truncated = True
                    break
                counts["scanned_sessions"] += 1
                try:
                    status = AgentSnapshot.model_validate(row["snapshot"]).status
                except ValueError:
                    findings["ModelRetryUpgradeInvalidState"] += 1
                    counts["operator_review_sessions"] += 1
                    continue
                active = row["run_status"] not in {"succeeded", "failed", "cancelled"}
                checkpoint = None
                if row["projection"] is not None:
                    try:
                        checkpoint = ContextProjectionManifest.model_validate(
                            row["projection"]
                        ).checkpoint
                    except ValueError:
                        pass  # The existing pending-retry checks report invalid identity.
                for version in (1, 2, 3):
                    counts[f"v{version}_checkpoint_sessions"] += int(
                        checkpoint is not None
                        and checkpoint.checkpoint_version == version
                    )
                evidence_error, public_policy, evidence_policy = None, False, None
                try:
                    spec = next(
                        s
                        for s in WorkflowSpec.model_validate(row["run_spec"]).steps
                        if s.id == row["step_key"]
                    )
                    raw = spec.parameters.get("agent", {}).get(
                        "evidence_checkpoint", {}
                    )
                    public_policy = raw.get("mode") == "anchored_extracts" and any(
                        r.get("mode") == "approved_excerpt"
                        for r in raw.get("disclosure", {}).get("rules", [])
                    )
                    evidence_policy = EvidenceCheckpointPolicy.model_validate(raw)
                    evidence_error = evidence_policy.reservation_error
                except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
                    evidence_error = "AgentEvidencePolicyInvalid"
                v2_public = (
                    public_policy
                    and evidence_policy is not None
                    and evidence_policy.checkpoint_version == 2
                )
                counts["v2_public_excerpt_policy_sessions"] += int(v2_public)
                active_session = active and not status.is_terminal
                counts["active_unbound_public_policy_sessions"] += int(
                    active_session
                    and public_policy
                    and (
                        evidence_policy is None
                        or evidence_policy.disclosure.policy_version == 1
                    )
                )
                counts["evidence_reservation_blocked_sessions"] += int(
                    active_session and evidence_error is not None
                )
                if active_session and evidence_error:
                    findings[evidence_error] += 1
                invalid_contract = False
                saved_contract = None
                if row["contract"] is not None:
                    try:
                        saved_contract = ModelRequestContract.model_validate(
                            row["contract"]
                        )
                    except ValueError:
                        findings["ModelRequestContractInvalid"] += 1
                        invalid_contract = True
                lifecycle = (
                    model_lifecycle(saved_contract.model)
                    if saved_contract is not None
                    and saved_contract.adapter_version.startswith("deepseek.chat.")
                    else None
                )
                unusable = lifecycle in {"legacy", "unsupported"}
                counts["legacy_model_sessions"] += int(lifecycle == "legacy")
                counts["unsupported_model_sessions"] += int(lifecycle == "unsupported")
                counts["unbound_request_sessions"] += int(
                    row["call_count"] > 0 and row["contract"] is None
                )
                counts["terminal_historical_calls"] += (
                    row["call_count"] if status.is_terminal else 0
                )
                review = active_session and (
                    unusable or invalid_contract or evidence_error is not None
                )
                if active and not status.is_terminal and unusable:
                    findings[
                        "ModelDeprecated"
                        if lifecycle == "legacy"
                        else "ModelCapabilityUnsupported"
                    ] += 1
                inflight = active and status == "model_inflight"
                pending = (
                    active
                    and status == "ready_model"
                    and row["call_status"] == "failed"
                    and row["step_status"] in {"ready", "running"}
                )
                counts["model_inflight"] += int(inflight)
                counts["pending_retry_calls"] += int(pending)
                if inflight:
                    findings["ModelOutcomeUnknownOnRecovery"] += 1
                    review = True
                if not pending:
                    counts["operator_review_sessions"] += int(review)
                    continue
                counts["legacy_pending_retry_calls"] += int(lifecycle == "legacy")
                missing_projection = row["projection"] is None
                missing_request = row["contract"] is None
                counts["missing_projection_manifests"] += int(missing_projection)
                counts["missing_request_contracts"] += int(missing_request)
                blocked = (
                    missing_projection
                    or missing_request
                    or unusable
                    or invalid_contract
                    or evidence_error is not None
                )
                if missing_projection:
                    findings["AgentContextProjectionChanged"] += 1
                if missing_request:
                    findings["ModelRequestContractUnavailable"] += 1
                if not (missing_projection or missing_request or invalid_contract):
                    try:
                        manifest = ContextProjectionManifest.model_validate(
                            row["projection"]
                        )
                        saved_contract.verify_projection(manifest)
                    except (ValueError, ModelRequestError):
                        findings["ModelRequestContractInvalid"] += 1
                        blocked = True
                counts["retry_recovery_blocked"] += int(blocked)
                counts["operator_review_sessions"] += int(review or blocked)
            following = offset + counts["scanned_sessions"]
            return {
                "version": 2,
                **{
                    name: counts[name]
                    for name in (
                        "scanned_sessions",
                        "pending_retry_calls",
                        "missing_projection_manifests",
                        "missing_request_contracts",
                        "model_inflight",
                        "retry_recovery_blocked",
                        "legacy_model_sessions",
                        "unsupported_model_sessions",
                        "legacy_pending_retry_calls",
                        "unbound_request_sessions",
                        "terminal_historical_calls",
                        "operator_review_sessions",
                        "v1_checkpoint_sessions",
                        "v2_checkpoint_sessions",
                        "v2_public_excerpt_policy_sessions",
                        "v3_checkpoint_sessions",
                        "active_unbound_public_policy_sessions",
                        "evidence_reservation_blocked_sessions",
                    )
                },
                "scope": {
                    "limit": limit,
                    "offset": offset,
                    "truncated": truncated,
                    "next_offset": following
                    if truncated and following <= 1_000_000
                    else None,
                },
                "findings": [
                    {"code": code, "count": count}
                    for code, count in sorted(findings.items())
                ],
            }
        finally:
            db.rollback()
            if engine.dialect.name == "sqlite":
                db.execute(text("PRAGMA query_only=OFF"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    engine = None
    try:
        url = os.environ["AGENTFLOW_DATABASE_URL"]
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5}
            if url.startswith("postgresql")
            else {"timeout": 5},
        )
        report = audit(
            engine,
            limit=args.limit,
            offset=args.offset,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 2 if report["findings"] else 0
    except (KeyError, ValueError, TimeoutError, SQLAlchemyError):
        print(json.dumps({"error": "ModelRetryUpgradeAuditUnavailable"}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
