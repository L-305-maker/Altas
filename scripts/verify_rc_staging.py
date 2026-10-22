"""Opt-in readonly preflight; separately authorized, bounded synthetic staging.

Never migrates a database, controls a remote host, or calls a paid provider.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from rc_staging import (
    RCStagingEvidenceReport,
    StagingConfiguration,
    connect,
    deployment_identity,
    evaluate,
    host_identity,
    read_database_facts,
    report,
    require_execution,
)
from sqlalchemy import select, text

from agentflow.application.ports.model_request import request_digest
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.infrastructure.persistence.models import RunRow


def configuration(path, environment):
    path = Path(path)
    if path.stat().st_size > 32000:
        raise ValueError("StagingConfigurationRejected")
    config = StagingConfiguration.model_validate_json(path.read_bytes())
    if str(config.environment_id) != environment:
        raise ValueError("StagingEnvironmentMismatch")
    return config


def preflight(config, *, mode="audit"):
    started = datetime.now(timezone.utc)
    engine = connect(os.environ["AGENTFLOW_RC_DATABASE_URL"], mode)
    try:
        facts = read_database_facts(engine, config, mode=mode)
        identity = deployment_identity()
        errors = evaluate(config, facts, identity)
        return report(
            config, facts=facts, errors=errors, started=started, identity=identity
        ), errors
    finally:
        engine.dispose()


async def worker(seconds, *, config_path, environment, allowed, slot):
    require_execution(environment, allowed)
    config = configuration(config_path, environment)
    if not 1 <= seconds <= 3600 or not 0 <= slot < config.max_workers:
        raise ValueError("StagingLimitsRejected")
    identity = host_identity("worker")
    if identity != [h for h in config.hosts if h.role == "worker"][slot]:
        raise ValueError("StagingHostMismatch")
    _, errors = preflight(config, mode="worker")
    if errors:
        raise ValueError("StagingPreflightRejected")
    url = os.environ["AGENTFLOW_RC_DATABASE_URL"]
    engine = connect(url, "worker")
    job = None
    # Session-scoped locks bound concurrent fixture workers. Runtime ownership
    # remains the existing Store lease/token, never this diagnostic lock.
    namespace = int(request_digest(str(config.environment_id))[:7], 16)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
            acquired = False
            try:
                db.execute(text("SET statement_timeout = '5s'"))
                acquired = db.scalar(
                    text("SELECT pg_try_advisory_lock(:n, :slot)"),
                    {"n": namespace, "slot": slot},
                )
                if not acquired:
                    raise ValueError("StagingWorkerSlotOccupied")
                from rc_workload import serve

                job = asyncio.create_task(
                    serve(url, os.environ["AGENTFLOW_RC_RECORDS"], seconds)
                )
                while not job.done():
                    await asyncio.wait({job}, timeout=0.5)
                    if not job.done():
                        await asyncio.to_thread(db.execute, text("SELECT 1"))
                await job
            finally:
                failure = sys.exception()
                try:
                    if job is not None and not job.done():
                        job.cancel()
                        # Preserve Runner's existing five-second cleanup.
                        try:
                            await asyncio.wait_for(job, timeout=6)
                        except (asyncio.CancelledError, TimeoutError):
                            pass
                        except Exception:
                            if failure is None:
                                raise
                finally:
                    if acquired:
                        # Pool return/rollback does not release a session lock.
                        # Explicitly release while we still own the connection.
                        try:
                            if not db.scalar(
                                text("SELECT pg_advisory_unlock(:n, :slot)"),
                                {"n": namespace, "slot": slot},
                            ):
                                raise ValueError("StagingSlotReleaseUnknown")
                        except Exception:
                            with suppress(Exception):
                                db.invalidate()
                            if failure is None:
                                raise
    finally:
        failure = sys.exception()
        try:
            engine.dispose()
        except Exception:
            if failure is None:
                raise


def synthetic(config, args):
    from rc_workload import Workload

    plan = Path(args.plan)
    if not 1 <= args.seconds <= 600 or (
        os.name == "nt" and sys.version_info < (3, 12, 4)
    ):
        raise ValueError("StagingLimitsRejected")
    if args.command == "seed":
        if plan.exists() or not 1 <= args.runs <= 20:
            raise ValueError("StagingPlanRejected")
        # Require a fresh private directory (Windows 3.12.4+ installs its ACL).
        # Never trust a pre-existing shared directory for private fixture IDs.
        plan.parent.mkdir(mode=0o700)
        # A private operator file; contains only fixture IDs and scenario names.
        fd = os.open(plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        data = {
            "environment": str(config.environment_id),
            "batch_id": str(uuid4()),
            "runs": [],
        }
        plan.write_text(json.dumps(data), encoding="utf-8")
    else:
        if plan.stat().st_size > 8000:
            raise ValueError("StagingPlanRejected")
        data = json.loads(plan.read_text("utf-8"))
        if (
            data["environment"] != str(config.environment_id)
            or not 1 <= len(data["runs"]) <= 20
        ):
            raise ValueError("StagingPlanRejected")
    workload = Workload(
        os.environ["AGENTFLOW_RC_DATABASE_URL"],
        os.environ["AGENTFLOW_RC_RECORDS"],
        admission_policy=ProviderAdmissionPolicy.model_validate_json(
            os.environ["AGENTFLOW_RC_ADMISSION_POLICY"]
        )
        if os.environ.get("AGENTFLOW_RC_ADMISSION_POLICY")
        else None,
        observed_usage_policy=ObservedUsagePolicy.model_validate_json(
            os.environ["AGENTFLOW_RC_OBSERVED_USAGE_POLICY"]
        )
        if os.environ.get("AGENTFLOW_RC_OBSERVED_USAGE_POLICY")
        else None,
    )
    try:
        workload.deadline = time.monotonic() + args.seconds
        if args.command == "seed":
            for _ in range(args.runs):
                if time.monotonic() >= workload.deadline:
                    raise ValueError("StagingOperationTimeout")
                run = workload.submit(args.scenario, staging_batch=data["batch_id"])
                data["runs"].append({"id": run, "scenario": args.scenario})
                plan.write_text(json.dumps(data), encoding="utf-8")
        else:
            for entry in data["runs"]:
                if time.monotonic() >= workload.deadline:
                    raise ValueError("StagingOperationTimeout")
                run = str(UUID(entry["id"]))
                current = workload.store.get_run(run)
                with workload.store.transaction() as db:
                    inputs = db.scalar(select(RunRow.inputs).where(RunRow.id == run))
                if inputs != {
                    "scenario": entry["scenario"],
                    "rc_staging_batch": str(UUID(data["batch_id"])),
                } or not current["spec"]["name"].startswith(f"rc-{entry['scenario']}-"):
                    raise ValueError("StagingPlanRejected")
                if args.command == "cancel":
                    workload.store.cancel(run)
                elif args.command == "approve":
                    workload.approve(run)
                else:
                    workload.reconcile(run)
        return len(data["runs"])
    finally:
        workload.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("identity", "preflight", "seed", "approve", "reconcile", "cancel"),
    )
    parser.add_argument("--role", choices=("api", "worker"), default="worker")
    parser.add_argument("--config")
    parser.add_argument("--environment-id")
    parser.add_argument("--allow-staging-execution", action="store_true")
    parser.add_argument("--read-staging", action="store_true")
    parser.add_argument("--plan")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument(
        "--scenario",
        choices=(
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
        ),
        default="normal",
    )
    args = parser.parse_args()
    try:
        if args.command == "identity":
            print(host_identity(args.role).model_dump_json())
            return 0
        if args.command != "preflight":
            require_execution(args.environment_id, args.allow_staging_execution)
        elif not args.read_staging:
            raise ValueError("StagingReadOptInRequired")
        config = configuration(args.config, args.environment_id)
        result, errors = preflight(
            config, mode="audit" if args.command == "preflight" else "worker"
        )
        if args.command == "preflight" or errors:
            print(result.model_dump_json())
            return 2 if errors else 0
        count = synthetic(config, args)
        evidence = RCStagingEvidenceReport.model_validate(
            {
                **result.model_dump(),
                "staging_authorization_state": "synthetic_execution",
                "completed_at": datetime.now(timezone.utc),
            }
        )
        print(
            json.dumps(
                {
                    "evidence_report": evidence.model_dump(mode="json"),
                    "synthetic_runs": count,
                    "review_required": True,
                }
            )
        )
        return 0
    except (Exception, KeyboardInterrupt):
        # Never print input, driver exceptions, credentials or traceback.
        code = (
            "StagingAuthorizationRequired"
            if args.command != "preflight"
            and (
                not args.allow_staging_execution
                or not os.environ.get("AGENTFLOW_RC_STAGING_AUTHORIZATION")
            )
            else "StagingCheckFailed"
        )
        print(json.dumps({"status": "FAIL", "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
