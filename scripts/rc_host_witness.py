"""Protected, unsigned host observations from actual RC execution boundaries.

No event-import or private-key interface. Files are provenance, not authority:
an independent protected signer must verify the collector and fault binding.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field
from rc_acceptance import Digest, Frozen, Operation
from rc_acceptance_db import checked_plan, collect_snapshot, database_identity
from rc_causal import (
    CausalChallenge,
    CausalHost,
    CausalSignedEvidence,
    RCCausalWitness,
    causal_group,
    verify_causal_acceptance,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from verify_rc_acceptance import (
    bounded_json,
    context,
    matches,
    reject_constant,
    unique_fields,
)

from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    LostLease,
)
from agentflow.application.ports.model_request import canonical_json, request_digest
from agentflow.infrastructure.persistence.models import RunRow, StepRow

MAX_EVENTS = 200
MAX_BYTES = 1_000_000


class CollectorHeader(Frozen):
    version: Literal[1] = 1
    challenge_digest: Digest
    environment_id: UUID
    staging_batch_digest: Digest
    case_id: Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    host_identity: Digest
    process_id: int = Field(strict=True, ge=1)
    process_nonce: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    deployment_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    collector_digest: Digest
    initial_snapshot_digest: Digest
    started_at: AwareDatetime


class JournalSeal(Frozen):
    events: int = Field(strict=True, ge=0, le=MAX_EVENTS)
    journal_digest: Digest
    complete: bool = Field(strict=True)


class Observation(Frozen):
    operation: Operation
    target_step_ref: Digest
    action_ref: Digest | None = None
    model_call_ref: Digest | None = None
    attempt_ref: Digest | None = None
    provider_ref: Digest | None = None
    permit_ref: Digest | None = None
    approval_ref: Digest | None = None
    reconciliation_ref: Digest | None = None
    lease_digest: Digest | None = None
    # Error bound is zero for the DB observation clock, nonzero only for a
    # recently calibrated host-clock observation when the DB cannot be read.
    clock_error_seconds: float = Field(ge=0, le=2, allow_inf_nan=False)
    source: Literal[
        "store.claim",
        "agent.load",
        "agent.reservation",
        "agent.admission_deferred",
        "agent.confirmed",
        "tool.entry",
        "agent.lost_lease",
        "database.error",
        "worker.stop",
        "supervisor.exit",
        "database.probe",
        "database.backpressure",
    ]
    snapshot_digest: Digest | None = None
    # Parent-only binding to the observed child header and verified prefix.
    # This stays in the fault-bound private journal, not the v3 operation schema.
    source_record_digest: Digest | None = None


class ProvenanceBinding(Frozen):
    version: Literal[1] = 1
    challenge_digest: Digest
    case_id: Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    # Each entry is (pinned host identity, sealed journal digest).
    journals: tuple[tuple[Digest, Digest], ...] = Field(min_length=2, max_length=3)
    fault_record_digest: Digest


def check_registration(directory, challenge):
    root = Path(directory)
    if root.is_symlink() or (os.name != "nt" and root.stat().st_mode & 0o077):
        raise ValueError("PrivateChallengeRegistryRequired")
    if (root / "consumed.json").exists():
        raise ValueError("ChallengeReplay")
    if (
        CausalChallenge.model_validate(bounded_json(root / "issued.json", 16000))
        != challenge
    ):
        raise ValueError("ChallengeRegistrationMismatch")
    if not challenge.issued_at <= datetime.now(timezone.utc) <= challenge.expires_at:
        raise ValueError("EvidenceExpired")


def _write(path, value, maximum=16000):
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError("WitnessBoundsExceeded")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return len(encoded)


class HostCollector:
    """One process, one case, one fresh private directory; no Runtime writes."""

    def __init__(
        self,
        store,
        plan,
        challenge,
        registry,
        root,
        host,
        case,
        *,
        target=None,
        seconds=60,
        process_nonce=None,
    ):
        if not 1 <= seconds <= 600 or case not in "ABCDEFGHIJ":
            raise ValueError("WitnessBoundsExceeded")
        if os.name == "nt" and sys.version_info < (3, 12, 4):
            raise ValueError("PrivateWitnessDirectoryRequired")
        check_registration(registry, challenge)
        if (
            checked_plan(plan, challenge.environment_id)
            != challenge.staging_batch_digest
        ):
            raise ValueError("WitnessScopeMismatch")
        self.store, self.plan, self.challenge, self.registry = (
            store,
            plan,
            challenge,
            registry,
        )
        self.host, self.case, self.target = host, case, target
        self.deadline = time.monotonic() + seconds
        self.lock = threading.RLock()
        self.broken, self.closed, self.count, self.bytes = False, False, 0, 0
        self.tasks, self.loaded_tasks, self.attempts = {}, set(), set()
        self.backpressure = set()
        root = Path(root)
        if root.is_symlink():
            raise ValueError("PrivateWitnessDirectoryRequired")
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.root = root
        first = self.snapshot()
        if target is not None and target not in {s.ref for s in first.steps}:
            raise ValueError("WitnessTargetMissing")
        self.attempts.update(a.ref for a in first.attempts)
        self.header = dict(
            version=1,
            challenge_digest=request_digest(challenge.model_dump(mode="json")),
            environment_id=str(challenge.environment_id),
            staging_batch_digest=challenge.staging_batch_digest,
            case_id=case,
            host_identity=host,
            process_id=os.getpid(),
            process_nonce=process_nonce or uuid4().hex,
            deployment_sha=challenge.deployment_sha,
            collector_digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            initial_snapshot_digest=request_digest(first.model_dump(mode="json")),
            started_at=first.observed_at.isoformat(),
        )
        self.header = CollectorHeader.model_validate(self.header).model_dump(
            mode="json"
        )
        self.last = request_digest(self.header)
        self.bytes += _write(root / "header.json", self.header)

    def ref(self, *parts):
        return request_digest(
            (
                str(self.challenge.environment_id),
                self.challenge.staging_batch_digest,
                parts,
            )
        )

    def snapshot(self):
        start = time.time()
        result = collect_snapshot(
            self.store.engine,
            self.plan,
            self.challenge.environment_id,
            evidence_version=3,
        )
        end = time.time()
        error = max(
            abs(start - result.observed_at.timestamp()),
            abs(end - result.observed_at.timestamp()),
        )
        if error > 2:
            raise ValueError("WitnessClockUnverified")
        self.calibration = (end, time.monotonic(), error)
        return result

    def isolated(self):
        # Existing Store.claim is deliberately not replaced by a second scheduler.
        # Staging must be dedicated to this bounded synthetic batch. Operators
        # must exclude other writers; this guard is not a database RLS policy.
        check_registration(self.registry, self.challenge)
        with Session(self.store.engine) as db:
            if db.scalar(
                select(func.count())
                .select_from(RunRow)
                .where(
                    RunRow.status.not_in(("succeeded", "failed", "cancelled")),
                    RunRow.id.not_in([r["id"] for r in self.plan["runs"]]),
                )
            ):
                raise ValueError("WitnessBatchIsolationRequired")

    def step(self, task):
        if task.run_id not in {r["id"] for r in self.plan["runs"]}:
            raise ValueError("WitnessScopeMismatch")
        with Session(self.store.engine) as db:
            identity = db.scalar(
                select(StepRow.id).where(
                    StepRow.run_id == task.run_id, StepRow.key == task.step_id
                )
            )
        if identity is None:
            raise ValueError("WitnessTargetMissing")
        return self.ref("step", identity)

    def _emit(self, observation):
        check_registration(self.registry, self.challenge)
        if self.closed or self.count >= MAX_EVENTS or time.monotonic() > self.deadline:
            raise ValueError("WitnessBoundsExceeded")
        value = dict(
            sequence=self.count,
            previous_digest=self.last,
            observation=observation.model_dump(mode="json"),
        )
        value["record_digest"] = request_digest(value)
        data = canonical_json(value).encode("utf-8")
        if self.bytes + len(data) > MAX_BYTES:
            raise ValueError("WitnessBoundsExceeded")
        self.bytes += _write(self.root / f"event-{self.count:04d}.json", value)
        self.last, self.count = value["record_digest"], self.count + 1

    def best_effort(self, callback, *args):
        with self.lock:
            if not self.broken:
                try:
                    callback(*args)
                except Exception:
                    # No exception text, no effect on the real method's return
                    # or error. Missing/failed evidence can never become PASS.
                    self.broken = True

    def _observation(self, snapshot, source, kind, outcome, step, **fields):
        error = fields.pop("error_code", None)
        lease_until = fields.pop("lease_until", None)
        at = fields.pop("database_at", snapshot.observed_at.timestamp())
        action = fields.get("action_ref")
        target = (
            self.challenge.database_identity_digest
            if kind.startswith("database_")
            else action
            if kind == "dispatch"
            else step
        )
        observation = Observation(
            operation=Operation(
                kind=kind,
                target=target,
                outcome=outcome,
                at=at,
                lease_until=lease_until,
                error_code=None if error == "ProviderAdmissionDeferred" else error,
            ),
            target_step_ref=step,
            source=source,
            clock_error_seconds=0.0,
            snapshot_digest=request_digest(snapshot.model_dump(mode="json")),
            **fields,
        )
        # Admission's safe code belongs to v3 only; retained outside the v2
        # Operation shape as an explicit, typed source in the local journal.
        self._emit(observation)

    def claimed(self, task, lease_seconds=30):
        snapshot = self.snapshot()
        if task is None:
            step = next((s for s in snapshot.steps if s.ref == self.target), None)
            if (
                step
                and step.status == "running"
                and step.lease_until > snapshot.observed_at.timestamp()
            ):
                self._observation(
                    snapshot,
                    "store.claim",
                    "claim",
                    "rejected",
                    step.ref,
                    lease_digest=step.lease_digest,
                    lease_until=step.lease_until,
                )
            elif step and step.agent_status == "waiting_reconciliation":
                self._observation(
                    snapshot, "store.claim", "resume", "unavailable", step.ref
                )
            elif step and step.deferred_retry:
                attempt = next(
                    (
                        a
                        for a in reversed(snapshot.attempts)
                        if a.step_ref == step.ref and a.outcome == "failed"
                    ),
                    None,
                )
                pool = next(
                    (
                        p
                        for p in snapshot.providers
                        if attempt and p.ref == attempt.provider_ref
                    ),
                    None,
                )
                if (
                    attempt
                    and attempt.ref in self.backpressure
                    and pool
                    and min(step.available_at, pool.cooldown_until)
                    > snapshot.observed_at.timestamp()
                ):
                    self._observation(
                        snapshot,
                        "database.backpressure",
                        "rate_limited",
                        "rejected",
                        step.ref,
                        model_call_ref=attempt.call_ref,
                        attempt_ref=attempt.ref,
                        provider_ref=pool.ref,
                    )
            return  # No target lease evidence: no invented rejection.
        ref = self.step(task)
        lease = self.ref("lease", task.token)
        step = next(s for s in snapshot.steps if s.ref == ref)
        if step.lease_digest != lease or step.status != "running":
            raise ValueError("WitnessCommitUnconfirmed")
        self.tasks[lease] = (task, ref)  # Token stays in process memory only.
        self._observation(
            snapshot,
            "store.claim",
            "claim",
            "accepted",
            ref,
            lease_digest=lease,
            lease_until=step.lease_until,
            database_at=step.lease_until - lease_seconds,
        )

    def state(self, task, state, source, admission=None, backpressure=False):
        lease = self.ref("lease", task.token)
        if lease not in self.tasks:
            raise ValueError("WitnessTaskUnobserved")
        step = self.tasks[lease][1]
        snapshot = self.snapshot()
        session = state.snapshot.id
        if backpressure and state.model_attempts:
            attempt = state.model_attempts[-1]
            if attempt.outcome == "failed":
                self.backpressure.add(
                    self.ref(
                        "attempt", session, attempt.model_call_id, attempt.attempt_index
                    )
                )
        if source == "agent.load":
            if lease in self.loaded_tasks:
                return
            self.loaded_tasks.add(lease)
            fields = {}
            applied = next(
                (r for r in reversed(state.reconciliations) if r.applied), None
            )
            approval = next(
                (a for a in reversed(state.approvals) if a.consumed_at is not None),
                None,
            )
            if applied:
                fields = dict(
                    action_ref=self.ref("action", session, applied.action_id),
                    reconciliation_ref=self.ref(
                        "reconciliation", session, applied.request_id
                    ),
                )
            elif approval:
                fields = dict(
                    action_ref=self.ref("action", session, approval.action_id),
                    approval_ref=self.ref("approval", approval.approval_id),
                )
            self._observation(
                snapshot,
                source,
                "resume",
                "accepted",
                step,
                lease_digest=lease,
                **fields,
            )
        if admission == "admission_deferred":
            self._observation(
                snapshot,
                "agent.admission_deferred",
                "model_reserve",
                "unavailable",
                step,
                provider_ref=self.ref("provider", state.provider_key),
                lease_digest=lease,
            )
        for attempt in state.model_attempts if source == "agent.reservation" else ():
            ref = self.ref(
                "attempt", session, attempt.model_call_id, attempt.attempt_index
            )
            if ref not in self.attempts:
                self.attempts.add(ref)
                permit = next(
                    (
                        p
                        for p in state.model_permits
                        if (p.model_call_id, p.attempt_index)
                        == (attempt.model_call_id, attempt.attempt_index)
                    ),
                    None,
                )
                self._observation(
                    snapshot,
                    "agent.reservation",
                    "model_reserve",
                    "accepted",
                    step,
                    model_call_ref=self.ref("call", session, attempt.model_call_id),
                    attempt_ref=ref,
                    provider_ref=self.ref("provider", state.provider_key)
                    if state.provider_key
                    else None,
                    permit_ref=self.ref("permit", permit.id) if permit else None,
                    lease_digest=lease,
                    database_at=attempt.created_at,
                )
        if source == "agent.confirmed":
            if state.model_attempts and state.model_attempts[-1].outcome == "unknown":
                attempt = state.model_attempts[-1]
                self._observation(
                    snapshot,
                    source,
                    "model_reserve",
                    "unavailable",
                    step,
                    model_call_ref=self.ref("call", session, attempt.model_call_id),
                    attempt_ref=self.ref(
                        "attempt", session, attempt.model_call_id, attempt.attempt_index
                    ),
                    provider_ref=self.ref("provider", state.provider_key)
                    if state.provider_key
                    else None,
                    lease_digest=lease,
                )
            for action in state.snapshot.actions:
                if action.status.value == "unknown":
                    self._observation(
                        snapshot,
                        source,
                        "dispatch",
                        "unavailable",
                        step,
                        action_ref=self.ref("action", session, action.id),
                        lease_digest=lease,
                    )

    def dispatched(self, task, invocation):
        lease = self.ref("lease", task.token)
        actual = self.tasks.get(lease)
        if actual is None:
            raise ValueError("WitnessTaskUnobserved")
        snapshot = self.snapshot()
        from agentflow.infrastructure.persistence.agent_store import SQLAgentStore

        state = SQLAgentStore(self.store).load(task)
        stored = next(a for a in state.actions if a.action.id == invocation.action_id)
        if (
            stored.action.status.value != "inflight"
            or stored.idempotency_key != invocation.idempotency_key
        ):
            raise ValueError("WitnessDispatchUnconfirmed")
        self._observation(
            snapshot,
            "tool.entry",
            "dispatch",
            "accepted",
            actual[1],
            action_ref=self.ref("action", state.snapshot.id, invocation.action_id),
            lease_digest=lease,
        )

    def unavailable(self, task=None, *, stopped=False):
        # Host time is usable only after recent DB calibration, with an explicit
        # conservative uncertainty interval; no made-up DB timestamp.
        wall, mono, error = self.calibration
        if (
            time.monotonic() - mono > 30
            or abs((time.time() - wall) - (time.monotonic() - mono)) > 0.1
        ):
            raise ValueError("WitnessClockUnverified")
        step = (
            self.tasks.get(self.ref("lease", task.token), (None, self.target))[1]
            if task
            else self.target
        )
        if step is None:
            raise ValueError("WitnessTargetMissing")
        self._emit(
            Observation(
                operation=Operation(
                    kind="database_unavailable",
                    target=self.challenge.database_identity_digest,
                    outcome="safe_stopped" if stopped else "unavailable",
                    at=time.time(),
                    error_code="DatabaseUnavailable",
                ),
                target_step_ref=step,
                source="worker.stop" if stopped else "database.error",
                clock_error_seconds=min(2.0, error + 0.1),
            )
        )

    def stopped(self):
        # Called only after the owning loop has stopped following a DB error.
        self.unavailable(stopped=True)

    def child_dispatch(self, child, directory, nonce):
        """Observe the owned, still-live child before trusting an exit receipt.

        A killed child's unsealed journal is never promoted to complete evidence.
        Only this parent's independently observed exit can enter its own journal.
        """
        if child.poll() is not None:
            return None
        root = Path(directory)
        if root.is_symlink() or (root / "header.json").is_symlink():
            raise ValueError("PrivateWitnessDirectoryRequired")
        header = CollectorHeader.model_validate(
            bounded_json(root / "header.json", 16000)
        )
        if (
            header.process_id != child.pid
            or header.process_nonce != nonce
            or header.challenge_digest != self.header["challenge_digest"]
            or header.host_identity != self.host
            or header.collector_digest != self.header["collector_digest"]
            or header.case_id != self.case
        ):
            raise ValueError("WitnessProvenanceMismatch")
        last = request_digest(header.model_dump(mode="json"))
        dispatch = None
        size = (root / "header.json").stat().st_size
        for index in range(MAX_EVENTS):
            path = root / f"event-{index:04d}.json"
            if not path.exists():
                break
            size += path.stat().st_size
            if path.is_symlink() or size > MAX_BYTES:
                raise ValueError("WitnessBoundsExceeded")
            value = bounded_json(path, 16000)
            digest = value.pop("record_digest")
            if (
                set(value) != {"sequence", "previous_digest", "observation"}
                or type(value["sequence"]) is not int
                or value["sequence"] != index
                or value["previous_digest"] != last
                or request_digest(value) != digest
            ):
                raise ValueError("WitnessIncomplete")
            observed = Observation.model_validate(value["observation"])
            if (
                observed.source == "tool.entry"
                and observed.operation.outcome == "accepted"
            ):
                dispatch = observed
            last = digest
        if dispatch is None or child.poll() is not None:
            return None
        snapshot = self.snapshot()
        action = next(
            (a for a in snapshot.actions if a.ref == dispatch.action_ref), None
        )
        step = next(s for s in snapshot.steps if s.ref == dispatch.target_step_ref)
        if (
            action
            and action.status == "inflight"
            and step.lease_digest == dispatch.lease_digest
        ):
            return Observation.model_validate(
                {
                    **dispatch.model_dump(),
                    "source_record_digest": request_digest(
                        (header.model_dump(mode="json"), last)
                    ),
                }
            )
        return None

    def supervise(self, child, directory, nonce):
        # No remote PID or fault command interface. The caller owns this Popen.
        observed = None
        while child.poll() is None and time.monotonic() < self.deadline + 7:
            if observed is None:
                try:
                    observed = self.child_dispatch(child, directory, nonce)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass  # An exclusive file can still be in the middle of writing.
                except Exception:
                    self.broken = True
            time.sleep(0.05)
        if child.poll() is None:
            self.broken = True  # Bound cleanup to this invocation's owned process.
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            return
        if observed is not None and not self.broken:
            snapshot = self.snapshot()
            step = next(s for s in snapshot.steps if s.ref == observed.target_step_ref)
            action = next(a for a in snapshot.actions if a.ref == observed.action_ref)
            if (
                action.status == "inflight"
                and step.lease_digest == observed.lease_digest
                and step.lease_until > snapshot.observed_at.timestamp()
            ):
                self._observation(
                    snapshot,
                    "supervisor.exit",
                    "worker_exit",
                    "safe_stopped",
                    step.ref,
                    action_ref=action.ref,
                    lease_digest=step.lease_digest,
                    source_record_digest=observed.source_record_digest,
                )

    def rejected(self, task, writing):
        lease = self.ref("lease", task.token)
        if lease not in self.tasks:
            raise ValueError("WitnessTaskUnobserved")
        snapshot = self.snapshot()
        step = next(s for s in snapshot.steps if s.ref == self.tasks[lease][1])
        if (
            step.status == "running"
            and step.run_status not in {"succeeded", "failed", "cancelled"}
            and step.lease_digest == lease
            and step.lease_until > snapshot.observed_at.timestamp()
        ):
            raise ValueError("WitnessRejectionUnconfirmed")
        self._observation(
            snapshot,
            "agent.lost_lease",
            ("cancelled_commit" if step.run_status == "cancelled" else "stale_commit")
            if writing
            else "resume",
            "rejected",
            step.ref,
            error_code="LostLease",
            lease_digest=lease,
        )

    def recovered(self):
        snapshot = self.snapshot()  # Real successful query of the same DB.
        if self.target:
            self._observation(
                snapshot,
                "database.probe",
                "database_recovered",
                "accepted",
                self.target,
            )

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                _write(
                    self.root / "closed.json",
                    dict(
                        events=self.count,
                        journal_digest=self.last,
                        complete=not self.broken,
                    ),
                )
            except Exception:
                self.broken = True


class ObservedStore:
    def __init__(self, store, collector):
        self.delegate, self.collector = store, collector

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def claim(self, lease_seconds=30):
        try:
            self.collector.isolated()  # Execution authorization, not logging.
            task = self.delegate.claim(lease_seconds)
        except SQLAlchemyError:
            self.collector.best_effort(self.collector.unavailable)
            raise
        self.collector.best_effort(self.collector.claimed, task, lease_seconds)
        return task


class ObservedAgents:
    def __init__(self, agents, collector):
        self.delegate, self.collector = agents, collector

    def __getattr__(self, name):
        if name not in {
            "load",
            "commit_transition",
            "complete_agent_step",
            "fail_agent_step",
            "suspend_agent_step",
            "reserve_model_attempt",
            "defer_model_retry",
        }:
            return getattr(self.delegate, name)

        def invoke(task, *args, **kwargs):
            try:
                answer = getattr(self.delegate, name)(task, *args, **kwargs)
            except LostLease:
                self.collector.best_effort(
                    self.collector.rejected, task, name != "load"
                )
                raise
            except (AgentStoreUnavailable, SQLAlchemyError):
                self.collector.best_effort(self.collector.unavailable, task)
                raise
            state = answer.state if name == "reserve_model_attempt" else answer
            reservation = name == "reserve_model_attempt" or (
                name == "commit_transition"
                and args
                and args[0].snapshot.status.value == "model_inflight"
                and args[0].model_result is None
            )
            self.collector.best_effort(
                self.collector.state,
                task,
                state,
                "agent.load"
                if name == "load"
                else "agent.reservation"
                if reservation
                else "agent.confirmed",
                answer.status if name == "reserve_model_attempt" else None,
                bool(
                    name == "defer_model_retry"
                    and args
                    and args[0].model_result
                    and args[0].model_result.backpressure is not None
                ),
            )
            return answer

        return invoke


def read_journal(root, challenge, registry):
    check_registration(registry, challenge)
    root = Path(root)
    if root.is_symlink() or (os.name != "nt" and root.stat().st_mode & 0o077):
        raise ValueError("PrivateWitnessDirectoryRequired")
    names = set()
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_EVENTS + 4 or not entry.is_file(follow_symlinks=False):
                raise ValueError("WitnessBoundsExceeded")
            names.add(entry.name)
    header = CollectorHeader.model_validate(
        bounded_json(root / "header.json", 16000)
    ).model_dump(mode="json")
    if (
        header["challenge_digest"] != request_digest(challenge.model_dump(mode="json"))
        or header["environment_id"] != str(challenge.environment_id)
        or header["staging_batch_digest"] != challenge.staging_batch_digest
        or header["deployment_sha"] != challenge.deployment_sha
    ):
        raise ValueError("WitnessScopeMismatch")
    closed = JournalSeal.model_validate(
        bounded_json(root / "closed.json", 16000)
    ).model_dump()
    if (
        closed.get("complete") is not True
        or type(closed.get("events")) is not int
        or not 0 <= closed["events"] <= MAX_EVENTS
    ):
        raise ValueError("WitnessIncomplete")
    last, observations, size = request_digest(header), [], 0
    for index in range(closed["events"]):
        path = root / f"event-{index:04d}.json"
        if path.is_symlink():
            raise ValueError("WitnessIncomplete")
        value = bounded_json(path, 16000)
        size += path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("WitnessBoundsExceeded")
        digest = value.pop("record_digest")
        if (
            set(value) != {"sequence", "previous_digest", "observation"}
            or type(value["sequence"]) is not int
            or value["sequence"] != index
            or value["previous_digest"] != last
            or digest != request_digest(value)
        ):
            raise ValueError("WitnessIncomplete")
        observations.append(Observation.model_validate(value["observation"]))
        last = digest
    if last != closed["journal_digest"] or names - {"unsigned-host.json"} != {
        "header.json",
        "closed.json",
    } | {f"event-{i:04d}.json" for i in range(closed["events"])}:
        raise ValueError("WitnessIncomplete")
    return header, observations, last


def unsigned_draft(root, challenge, registry, trust, receipts, binding):
    header, observations, digest = read_journal(root, challenge, registry)
    case = header["case_id"]
    if (
        binding.challenge_digest != header["challenge_digest"]
        or binding.case_id != case
        or (header["host_identity"], digest) not in binding.journals
        or len(set(h for h, _ in binding.journals)) != len(binding.journals)
    ):
        raise ValueError("WitnessProvenanceMismatch")
    report = verify_causal_acceptance(challenge, trust, receipts)
    result = next(c for c in report.cases if c.case_id == case)
    if result.observed_invariants:
        raise ValueError("WitnessDatabaseConflict")
    facts = [
        r
        for r in receipts
        if r.evidence.payload.source == "database"
        and r.evidence.payload.case_id == case
    ]
    before = next(
        r.evidence.payload.snapshot
        for r in facts
        if r.evidence.payload.phase == "before"
    )
    after = next(
        r.evidence.payload.snapshot
        for r in facts
        if r.evidence.payload.phase == "after"
    )
    faults = [
        r
        for r in receipts
        if r.evidence.payload.source == "fault" and r.evidence.payload.case_id == case
    ]
    if len(faults) != 1 or faults[
        0
    ].evidence.payload.protected_record_digest != request_digest(
        binding.model_dump(mode="json")
    ):
        raise ValueError("WitnessProvenanceMismatch")
    fault = faults[0]
    operations = []
    selected = []
    kinds = {
        "A": {"claim"},
        "B": {"worker_exit", "resume", "dispatch"},
        "C": {"claim", "stale_commit"},
        "D": {"resume", "dispatch"},
        "E": {"resume", "dispatch"},
        "F": {"model_reserve"},
        "G": {"rate_limited", "model_reserve"},
        "H": {"resume", "model_reserve", "dispatch"},
        "I": {"cancelled_commit", "stale_commit", "resume", "dispatch"},
        "J": {"database_unavailable", "database_recovered", "worker_exit", "resume"},
    }[case] | {
        "stale_commit",
        "cancelled_commit",
        "unknown_dispatch",
        "database_unavailable",
    }
    for observed in observations:
        op = observed.operation
        if op.kind not in kinds:
            continue  # Prelude/context remains in the signed fault-bound journal.
        low, high = (
            op.at - observed.clock_error_seconds,
            op.at + observed.clock_error_seconds,
        )
        if (
            high < fault.evidence.started_at.timestamp()
            or low > fault.evidence.completed_at.timestamp()
        ):
            continue
        if not (
            fault.evidence.started_at.timestamp()
            <= op.at - observed.clock_error_seconds
            <= op.at + observed.clock_error_seconds
            <= fault.evidence.completed_at.timestamp()
        ):
            raise ValueError("WitnessClockUnverified")
        values = {
            **op.model_dump(mode="json"),
            **observed.model_dump(
                mode="json",
                exclude={
                    "operation",
                    "source",
                    "clock_error_seconds",
                    "snapshot_digest",
                    "source_record_digest",
                },
            ),
            "evidence_version": 3,
            "environment_id": header["environment_id"],
            "staging_batch_digest": header["staging_batch_digest"],
            "case_id": case,
            "host_identity": header["host_identity"],
            "causal_group_id": causal_group(
                challenge, case, before, after, fault.evidence_digest
            ),
            "database_before_digest": request_digest(before.model_dump(mode="json")),
            "database_after_digest": request_digest(after.model_dump(mode="json")),
            "fault_receipt_digest": fault.evidence_digest,
        }
        if observed.source == "agent.admission_deferred":
            values["error_code"] = "ProviderAdmissionDeferred"
        operations.append(
            RCCausalWitness(**values, witness_digest=request_digest(values))
        )
        selected.append(observed)
    payload = CausalHost(
        case_id=case, host_digest=header["host_identity"], operations=operations
    )
    if not operations:
        raise ValueError("WitnessIncomplete")
    return dict(
        evidence_version=3,
        challenge=challenge.model_dump(mode="json"),
        started_at=(
            datetime.fromtimestamp(
                min(o.operation.at - o.clock_error_seconds for o in selected),
                timezone.utc,
            )
            - timedelta(microseconds=1)
        ).isoformat(),
        completed_at=(
            datetime.fromtimestamp(
                max(o.operation.at + o.clock_error_seconds for o in selected),
                timezone.utc,
            )
            + timedelta(microseconds=1)
        ).isoformat(),
        payload=payload.model_dump(mode="json"),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("identity", "preflight", "status", "probe", "run", "export", "local"),
    )
    for name in (
        "config",
        "environment-id",
        "v1-report",
        "plan",
        "challenge",
        "state-dir",
        "records-root",
        "journal",
        "binding",
        "trust",
        "trust-sha256",
        "target-step-ref",
        "bin",
    ):
        parser.add_argument("--" + name)
    parser.add_argument("--case", choices=tuple("ABCDEFGHIJ"))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--slot", type=int, default=0)
    parser.add_argument("--read-staging", action="store_true")
    parser.add_argument("--allow-staging-execution", action="store_true")
    parser.add_argument("--worker-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        from rc_staging import connect, host_identity, require_execution
        from verify_rc_staging import preflight

        if args.command == "identity":
            print(host_identity("worker").model_dump_json())
            return 0
        if args.command == "local":
            from rc_support import ROOT, TemporaryPostgres

            with TemporaryPostgres(args.bin) as pg:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "tests/integration/test_rc_host_witness.py",
                        "-q",
                        "--tb=short",
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "AGENTFLOW_TEST_POSTGRES_URL": pg.database(
                            "agentflow_rc_source"
                        ),
                    },
                    capture_output=True,
                    timeout=180,
                )
            print(
                json.dumps(
                    dict(
                        status="PASS" if result.returncode == 0 else "FAIL",
                        counts=[
                            n.decode("ascii")
                            for n in re.findall(
                                rb"\d+ (?:passed|failed|skipped)", result.stdout[-8192:]
                            )
                        ],
                        cross_host="NOT VERIFIED",
                        signed=False,
                    )
                )
            )
            return 0 if result.returncode == 0 else 2
        executing = args.command in {"probe", "run"}
        if executing:
            require_execution(args.environment_id, args.allow_staging_execution)
        elif args.command in {"preflight", "status"} and not args.read_staging:
            raise ValueError("StagingReadOptInRequired")
        config, original, plan, _ = context(args)
        if str(config.environment_id) != args.environment_id:
            raise ValueError("WitnessScopeMismatch")
        checked, errors = (
            preflight(config, mode="worker" if executing else "audit")
            if args.command != "export"
            else (None, ())
        )
        if errors:
            raise ValueError("StagingPreflightRejected")
        if args.command == "preflight":
            print(checked.model_dump_json())
            return 0
        challenge = CausalChallenge.model_validate(bounded_json(args.challenge, 16000))
        matches(challenge, config, original, checked_plan(plan, config.environment_id))
        check_registration(args.state_dir, challenge)
        if args.command == "export":
            from rc_acceptance import AcceptanceTrust

            path = Path(args.trust)
            with path.open("rb") as stream:
                trust_bytes = stream.read(32001)
            if (
                len(trust_bytes) > 32000
                or hashlib.sha256(trust_bytes).hexdigest() != args.trust_sha256
            ):
                raise ValueError("TrustPinMismatch")
            trust = AcceptanceTrust.model_validate(
                json.loads(
                    trust_bytes,
                    object_pairs_hook=unique_fields,
                    parse_constant=reject_constant,
                )
            )
            binding = ProvenanceBinding.model_validate(bounded_json(args.binding, 8000))
            # Bounded, independently signed before/after/fault files live beside
            # the binding. No free-form operation input or signing key accepted.
            receipts = [
                CausalSignedEvidence.model_validate(
                    bounded_json(Path(args.binding).parent / name, 2_100_000)
                )
                for name in ("before.json", "after.json", "fault.json")
            ]
            draft = unsigned_draft(
                args.journal, challenge, args.state_dir, trust, receipts, binding
            )
            worker_hosts = {h.host_digest for h in config.hosts if h.role == "worker"}
            if draft["payload"]["host_digest"] not in worker_hosts:
                raise ValueError("StagingHostMismatch")
            _write(Path(args.journal) / "unsigned-host.json", draft, MAX_BYTES)
            print(
                json.dumps(
                    dict(
                        status="NOT_VERIFIED",
                        unsigned=True,
                        events=len(draft["payload"]["operations"]),
                    )
                )
            )
            return 1
        engine = connect(
            os.environ.get("AGENTFLOW_RC_DATABASE_URL", ""),
            "worker" if executing else "audit",
        )
        try:
            if (
                database_identity(config, engine.url)
                != challenge.database_identity_digest
            ):
                raise ValueError("WitnessDatabaseMismatch")
            if args.command == "status":
                snapshot = collect_snapshot(
                    engine, plan, challenge.environment_id, evidence_version=3
                )
                print(
                    json.dumps(
                        dict(
                            status="NOT_VERIFIED",
                            snapshot_digest=request_digest(
                                snapshot.model_dump(mode="json")
                            ),
                            steps=len(snapshot.steps),
                        )
                    )
                )
                return 0
            host = host_identity("worker")
            workers = [h for h in config.hosts if h.role == "worker"]
            if (
                not 0 <= args.slot < len(workers)
                or host != workers[args.slot]
                or not args.case
            ):
                raise ValueError("StagingHostMismatch")
            approved = os.environ.get("AGENTFLOW_RC_WITNESS_ROOT")
            if (
                not approved
                or Path(approved).resolve() != Path(args.records_root).resolve()
            ):
                raise ValueError("PrivateWitnessDirectoryRequired")
            private_root = Path(approved)
            if (
                private_root.is_symlink()
                or not private_root.is_dir()
                or (os.name != "nt" and private_root.stat().st_mode & 0o077)
            ):
                raise ValueError("PrivateWitnessDirectoryRequired")
            if args.worker_child and (
                Path(os.environ["AGENTFLOW_RC_CHILD_JOURNAL"]).parent.resolve()
                != private_root.resolve()
                or not os.environ.get("AGENTFLOW_RC_CHILD_NONCE")
            ):
                raise ValueError("PrivateWitnessDirectoryRequired")
            from agentflow.infrastructure.persistence.store import Store

            store = Store(os.environ["AGENTFLOW_RC_DATABASE_URL"])
            collector = HostCollector(
                store,
                plan,
                challenge,
                args.state_dir,
                Path(os.environ["AGENTFLOW_RC_CHILD_JOURNAL"])
                if args.worker_child
                else Path(approved) / ("host-" + uuid4().hex),
                host.host_digest,
                args.case,
                target=args.target_step_ref,
                seconds=args.seconds,
                process_nonce=os.environ.get("AGENTFLOW_RC_CHILD_NONCE")
                if args.worker_child
                else None,
            )
            try:
                if args.command == "run" and not args.worker_child:
                    directory = Path(approved) / ("child-" + uuid4().hex)
                    nonce = uuid4().hex
                    # Fixed local entry; no arbitrary command, PID or remote host.
                    child = subprocess.Popen(
                        [
                            sys._base_executable,
                            str(Path(__file__).resolve()),
                            *sys.argv[1:],
                            "--worker-child",
                        ],
                        env={
                            **os.environ,
                            "PYTHONPATH": os.pathsep.join(sys.path),
                            "AGENTFLOW_RC_CHILD_JOURNAL": str(directory),
                            "AGENTFLOW_RC_CHILD_NONCE": nonce,
                        },
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        collector.supervise(child, directory, nonce)
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=5)
                            collector.broken = True
                else:
                    from verify_rc_staging import worker

                    if args.command == "probe" and args.case == "J":
                        collector.best_effort(collector.recovered)
                    try:
                        asyncio.run(
                            worker(
                                args.seconds,
                                config_path=args.config,
                                environment=args.environment_id,
                                allowed=args.allow_staging_execution,
                                slot=args.slot,
                                witness=collector,
                                claim_only=args.command == "probe",
                            )
                        )
                    except SQLAlchemyError:
                        collector.best_effort(collector.unavailable)
                        collector.best_effort(collector.stopped)
                        raise
            finally:
                collector.close()
                store.close()
            print(
                json.dumps(
                    dict(
                        status="NOT_VERIFIED",
                        unsigned=True,
                        events=collector.count,
                        complete=not collector.broken,
                    )
                )
            )
            return 1
        finally:
            engine.dispose()
    except Exception:
        print(
            json.dumps(dict(status="NOT_VERIFIED", error="WitnessCollectionRejected"))
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
