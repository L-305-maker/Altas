"""Bounded, redacted facts from one consistent READ ONLY database snapshot.

The caller must perform staging isolation/preflight before connecting externally.
SQLite is supported only for offline regression; it cannot attest PG row locks.
"""

import hashlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from rc_acceptance import (
    ActionFact,
    ArtifactFact,
    AttemptFact,
    DatabaseSnapshot,
    PermitFact,
    ProviderFact,
    StepFact,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from agentflow.application.agents.summary import (
    stored_summary_input,
    verify_summary_artifact,
)
from agentflow.application.agents.summary_consumption import stored_consumption
from agentflow.application.ports.model_request import canonical_json, request_digest
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentActionApprovalRow,
    AgentActionReconciliationRow,
    AgentActionRow,
    AgentMessageRow,
    AgentModelAttemptRow,
    AgentModelCallRow,
    AgentSessionRow,
    ArtifactRow,
    ProviderAdmissionRow,
    ProviderPermitRow,
    RunRow,
    StepRow,
)
from agentflow.infrastructure.persistence.store import json_bytes


def checked_plan(plan, environment):
    try:
        if (
            set(plan) != {"environment", "batch_id", "runs"}
            or str(UUID(plan["environment"])) != str(environment)
            or not 1 <= len(plan["runs"]) <= 20
        ):
            raise ValueError
        UUID(plan["batch_id"])
        for run in plan["runs"]:
            if set(run) != {"id", "scenario"} or run["scenario"] not in {
                "final",
                "normal",
                "review",
                "crash",
                "unknown",
                "retry",
                "cancel",
                "model_rate_limited",
                "summary",
                "summary_consumption",
            }:
                raise ValueError
            UUID(run["id"])
        if len({r["id"] for r in plan["runs"]}) != len(plan["runs"]):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("StagingPlanRejected") from None
    return request_digest(plan)


def database_identity(config, url):
    # No credentials. Bind endpoint as well as OID/environment; OIDs alone are
    # not globally unique. TLS/administrator marker still need live preflight.
    return request_digest(
        {
            "environment_id": str(config.environment_id),
            "oid": config.database_oid,
            "host": url.host,
            "port": url.port or 5432,
            "database": url.database,
        }
    )


def plan_run(db, plan, entry):
    """One batch definition shared by snapshots and execution authorization."""
    run = db.get(RunRow, entry["id"])
    if (
        run is None
        or run.inputs
        != {"scenario": entry["scenario"], "rc_staging_batch": plan["batch_id"]}
        or not run.spec["name"].startswith(f"rc-{entry['scenario']}-")
    ):
        raise ValueError("StagingPlanRejected")
    return run


def collect_snapshot(engine, plan, environment, *, evidence_version=2):
    if evidence_version not in (2, 3):
        raise ValueError("EvidenceVersionMismatch")
    if evidence_version == 3:
        from rc_causal import (
            ApprovalFact,
            CallFact,
            CausalAction,
            CausalAttempt,
            CausalSnapshot,
            ReconciliationFact,
            SummaryLink,
        )
    batch = checked_plan(plan, environment)

    def reference(*parts):
        return request_digest((str(environment), batch, parts))

    facts = {
        name: []
        for name in (
            "steps",
            "actions",
            "attempts",
            "artifacts",
            "permits",
            "providers",
        )
    }
    if evidence_version == 3:
        facts.update(calls=[], approvals=[], reconciliations=[], summary_links=[])
    invalid, measured = 0, 0
    deadline = time.monotonic() + 30
    with engine.connect() as connection:
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(text("SET LOCAL lock_timeout = '2s'"))
                stamp = float(
                    connection.scalar(
                        select(func.extract("epoch", func.clock_timestamp()))
                    )
                )
            elif engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only=ON"))
                connection.execute(text("BEGIN"))
                stamp = connection.scalar(
                    select((func.julianday("now") - 2440587.5) * 86400)
                )
            else:
                raise ValueError("UnsupportedEvidenceDatabase")
            with Session(bind=connection, autoflush=False) as db:
                agents = SQLAgentStore(SimpleNamespace())
                provider_keys = set()
                for entry in plan["runs"]:
                    run = plan_run(db, plan, entry)
                    specs = {
                        s.id: s for s in WorkflowSpec.model_validate(run.spec).steps
                    }
                    steps = db.scalars(
                        select(StepRow)
                        .where(StepRow.run_id == run.id)
                        .order_by(StepRow.id)
                        .limit(101)
                    ).all()
                    if len(steps) + len(facts["steps"]) > 100:
                        raise ValueError("EvidenceBoundsExceeded")
                    for step in steps:
                        if time.monotonic() > deadline:
                            raise TimeoutError("EvidenceDeadline")
                        ref = reference("step", step.id)
                        data = dict(
                            ref=ref,
                            run_ref=reference("run", run.id),
                            run_status=run.status,
                            status=step.status,
                            attempts=step.attempts,
                            available_at=step.available_at,
                            lease_digest=reference("lease", step.token)
                            if step.token
                            else None,
                            lease_until=step.lease_until,
                            artifact_required=specs[step.key].artifact,
                        )
                        artifacts = db.scalars(
                            select(ArtifactRow)
                            .where(ArtifactRow.step_id == step.id)
                            .limit(2)
                        ).all()
                        if len(artifacts) != int(
                            step.status == "succeeded" and specs[step.key].artifact
                        ):
                            invalid += 1
                        for artifact in artifacts:
                            if (
                                hashlib.sha256(json_bytes(artifact.content)).hexdigest()
                                != artifact.digest
                            ):
                                invalid += 1
                            facts["artifacts"].append(
                                ArtifactFact(step_ref=ref, digest=artifact.digest)
                            )
                        row = db.scalar(
                            select(AgentSessionRow).where(
                                AgentSessionRow.step_row_id == step.id
                            )
                        )
                        if row is not None:
                            # Bound child cardinality before the validating reader,
                            # which intentionally reads the complete session.
                            for table in (
                                AgentActionRow,
                                AgentMessageRow,
                                AgentModelCallRow,
                                AgentModelAttemptRow,
                                AgentActionApprovalRow,
                                AgentActionReconciliationRow,
                                ProviderPermitRow,
                            ):
                                if (
                                    db.scalar(
                                        select(func.count())
                                        .select_from(table)
                                        .where(table.session_id == row.id)
                                    )
                                    > 2000
                                ):
                                    raise ValueError("EvidenceBoundsExceeded")
                            state = agents._read(db, row, lock=False)
                            measured += len(
                                canonical_json(state.model_dump(mode="json")).encode(
                                    "utf-8"
                                )
                            )
                            if measured > 16_000_000:
                                raise ValueError("EvidenceBoundsExceeded")
                            snapshot = state.snapshot
                            data.update(
                                agent_status=snapshot.status.value,
                                revision=snapshot.revision,
                                agent_identity=request_digest(row.initial_snapshot),
                                snapshot_digest=request_digest(
                                    snapshot.model_dump(mode="json")
                                ),
                                counters=tuple(
                                    getattr(snapshot.budget, c)
                                    for c in (
                                        "model_rounds",
                                        "model_attempts",
                                        "tool_calls",
                                        "resource_units",
                                    )
                                ),
                                continuation=row.continuation_pending,
                                deferred_retry=step.error == "ModelRetryable",
                            )
                            if row.provider_key:
                                provider_keys.add(row.provider_key)
                            if evidence_version == 3:
                                for call in state.model_calls:
                                    facts["calls"].append(
                                        CallFact(
                                            ref=reference(
                                                "call", row.id, call.model_call_id
                                            ),
                                            step_ref=ref,
                                            round_index=call.round_index,
                                            attempts=call.attempts,
                                            status=call.status,
                                        )
                                    )
                                for approval in state.approvals:
                                    facts["approvals"].append(
                                        ApprovalFact(
                                            ref=reference(
                                                "approval", approval.approval_id
                                            ),
                                            action_ref=reference(
                                                "action", row.id, approval.action_id
                                            ),
                                            binding_digest=request_digest(
                                                approval.binding.model_dump(mode="json")
                                            ),
                                            decision=approval.decision,
                                            consumed_at=approval.consumed_at,
                                        )
                                    )
                                for reconciliation in state.reconciliations:
                                    facts["reconciliations"].append(
                                        ReconciliationFact(
                                            ref=reference(
                                                "reconciliation",
                                                row.id,
                                                reconciliation.request_id,
                                            ),
                                            action_ref=reference(
                                                "action",
                                                row.id,
                                                reconciliation.action_id,
                                            ),
                                            previous_status=reconciliation.previous_status.value,
                                            confirmed_status=reconciliation.confirmed_status,
                                            applied=reconciliation.applied,
                                            confirmed_at=reconciliation.created_at,
                                        )
                                    )
                            for stored in state.actions:
                                action = stored.action
                                approval = next(
                                    (
                                        a
                                        for a in state.approvals
                                        if a.action_id == action.id
                                    ),
                                    None,
                                )
                                reconciliation = next(
                                    (
                                        r
                                        for r in state.reconciliations
                                        if r.action_id == action.id and r.applied
                                    ),
                                    None,
                                )
                                identity = stored.model_dump(mode="json")
                                identity["action"].pop("status")
                                identity["action"].pop("error_code")
                                identity.pop("result")
                                facts["actions"].append(
                                    (
                                        CausalAction
                                        if evidence_version == 3
                                        else ActionFact
                                    )(
                                        **(
                                            {
                                                "binding_digest": request_digest(
                                                    stored.tool_binding.model_dump(
                                                        mode="json"
                                                    )
                                                )
                                                if stored.tool_binding
                                                else None,
                                                "idempotency_digest": request_digest(
                                                    stored.idempotency_key
                                                ),
                                            }
                                            if evidence_version == 3
                                            else {}
                                        ),
                                        ref=reference("action", row.id, action.id),
                                        step_ref=ref,
                                        status=action.status.value,
                                        identity_digest=request_digest(identity),
                                        outcome_digest=request_digest(
                                            stored.model_dump(mode="json")
                                        ),
                                        consumed_at=approval.consumed_at
                                        if approval
                                        else None,
                                        reconciled_at=reconciliation.created_at
                                        if reconciliation
                                        else None,
                                    )
                                )
                            for attempt in state.model_attempts:
                                facts["attempts"].append(
                                    (
                                        CausalAttempt
                                        if evidence_version == 3
                                        else AttemptFact
                                    )(
                                        **(
                                            {
                                                "summary_input_digest": attempt.request_contract.summary_input_digest
                                                if attempt.request_contract
                                                else None,
                                                "summary_consumption_digest": attempt.request_contract.summary_consumption_digest
                                                if attempt.request_contract
                                                else None,
                                                "contract_digest": attempt.request_contract.request_contract_digest
                                                if attempt.request_contract
                                                else None,
                                            }
                                            if evidence_version == 3
                                            else {}
                                        ),
                                        ref=reference(
                                            "attempt",
                                            row.id,
                                            attempt.model_call_id,
                                            attempt.attempt_index,
                                        ),
                                        step_ref=ref,
                                        call_ref=reference(
                                            "call", row.id, attempt.model_call_id
                                        ),
                                        index=attempt.attempt_index,
                                        outcome=attempt.outcome,
                                        outcome_digest=request_digest(
                                            attempt.model_dump(mode="json")
                                        ),
                                        identity_digest=request_digest(
                                            {
                                                "projection": attempt.context_projection.model_dump(
                                                    mode="json"
                                                )
                                                if attempt.context_projection
                                                else None,
                                                "contract": attempt.request_contract.model_dump(
                                                    mode="json"
                                                )
                                                if attempt.request_contract
                                                else None,
                                            }
                                        ),
                                        created_at=attempt.created_at,
                                        provider_ref=reference(
                                            "provider", row.provider_key
                                        )
                                        if row.provider_key
                                        else None,
                                    )
                                )
                            if (
                                specs[step.key].handler == "durable_summary"
                                and snapshot.status == "succeeded"
                            ):
                                if len(artifacts) != 1:
                                    invalid += 1
                                else:
                                    verify_summary_artifact(
                                        stored_summary_input(state),
                                        state,
                                        artifacts[0].content,
                                    )
                                    data["summary_identity"] = artifacts[0].digest
                            if (
                                len(state.messages) > 1
                                and "summary_consumption" in state.messages[1].payload
                            ):
                                consumption = stored_consumption(state)
                                if (
                                    agents._checked_consumption(db, run, step, state)
                                    != consumption
                                ):
                                    invalid += 1
                                data["consumer_identity"] = consumption.view.view_digest
                                if evidence_version == 3:
                                    generator = next(
                                        s
                                        for s in steps
                                        if s.key
                                        == consumption.policy.source_summary_step
                                    )
                                    generator_row = db.scalar(
                                        select(AgentSessionRow).where(
                                            AgentSessionRow.step_row_id == generator.id
                                        )
                                    )
                                    generator_state = agents._read(
                                        db, generator_row, lock=False
                                    )
                                    source = agents._checked_summary_input(
                                        db, run, generator, generator_state
                                    )
                                    generator_contracts = [
                                        a.request_contract
                                        for a in generator_state.model_attempts
                                        if a.outcome == "succeeded"
                                        and a.request_contract
                                    ]
                                    consumer_contracts = [
                                        a.request_contract
                                        for a in state.model_attempts
                                        if a.request_contract
                                    ]
                                    if generator_contracts and consumer_contracts:
                                        facts["summary_links"].append(
                                            SummaryLink(
                                                source_step_ref=reference(
                                                    "step",
                                                    source.source_snapshot.source_step_id,
                                                ),
                                                generator_step_ref=reference(
                                                    "step", generator.id
                                                ),
                                                consumer_step_ref=ref,
                                                source_snapshot_digest=source.manifest.source_snapshot_digest,
                                                summary_request_digest=source.manifest.request_id,
                                                summary_input_digest=source.manifest.summary_input_digest,
                                                artifact_digest=consumption.view.summary_artifact_digest,
                                                view_digest=consumption.view.view_digest,
                                                generator_contract_digest=generator_contracts[
                                                    -1
                                                ].request_contract_digest,
                                                consumer_contract_digest=consumer_contracts[
                                                    -1
                                                ].request_contract_digest,
                                            )
                                        )
                        try:
                            facts["steps"].append(StepFact(**data))
                        except ValueError:
                            invalid += 1
                if len(provider_keys) > 16:
                    raise ValueError("EvidenceBoundsExceeded")
                for key in sorted(provider_keys):
                    provider = db.get(ProviderAdmissionRow, key)
                    policy = ProviderAdmissionPolicy.model_validate(provider.policy)
                    facts["providers"].append(
                        ProviderFact(
                            ref=reference("provider", key),
                            max_concurrent=policy.max_concurrent_requests,
                            max_requests=policy.max_requests_per_window,
                            window_seconds=policy.window_seconds,
                            cooldown_until=provider.cooldown_until,
                        )
                    )
                    # Include other Runs sharing the quota, not only this batch.
                    permits = db.scalars(
                        select(ProviderPermitRow)
                        .where(ProviderPermitRow.provider_key == key)
                        .order_by(ProviderPermitRow.created_at, ProviderPermitRow.id)
                        .limit(2001)
                    ).all()
                    if len(permits) > 2000:
                        raise ValueError("EvidenceBoundsExceeded")
                    for permit in permits:
                        facts["permits"].append(
                            PermitFact(
                                ref=reference("permit", permit.id),
                                attempt_ref=reference(
                                    "attempt",
                                    permit.session_id,
                                    permit.model_call_id,
                                    permit.attempt_index,
                                ),
                                provider_ref=reference("provider", key),
                                status=permit.status,
                                created_at=permit.created_at,
                                expires_at=permit.expires_at,
                                released_at=permit.released_at,
                            )
                        )
                result = (
                    CausalSnapshot if evidence_version == 3 else DatabaseSnapshot
                )(
                    **(
                        {
                            "global_provider_scope": tuple(
                                reference("provider", key)
                                for key in sorted(provider_keys)
                            )
                        }
                        if evidence_version == 3
                        else {}
                    ),
                    observed_at=datetime.fromtimestamp(stamp, timezone.utc),
                    database_backend=engine.dialect.name,
                    invalid_records=invalid,
                    **{
                        k: tuple(r.model_dump(mode="json") for r in v)
                        for k, v in facts.items()
                    },
                )
                if len(result.model_dump_json().encode("utf-8")) > 2_000_000:
                    raise ValueError("EvidenceBoundsExceeded")
                return result
        finally:
            connection.rollback()
            if engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only=OFF"))
