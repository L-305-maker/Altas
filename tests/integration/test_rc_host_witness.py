"""Actual Store/Runner boundaries; PostgreSQL variants use independent engines."""

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest
from sqlalchemy import select
from test_durable_agent_runner import final, proposal
from test_durable_agent_runner import store as store

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import AgentStoreUnavailable, LostLease
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentSessionRow,
    ArtifactRow,
    StepRow,
)
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture
def witness(store, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import rc_host_witness as m
    from rc_acceptance import issue_challenge

    batch = "11111111-2222-4333-8444-555555555555"
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="rc-final-witness",
                steps=(
                    StepSpec(id="work", handler="agent", artifact=True, retry_delay=0),
                ),
            )
        ),
        {"scenario": "final", "rc_staging_batch": batch},
    )
    plan = {
        "environment": batch,
        "batch_id": batch,
        "runs": [{"id": run, "scenario": "final"}],
    }
    now = datetime.now(timezone.utc)
    challenge = m.CausalChallenge(
        environment_id=batch,
        deployment_sha="a" * 40,
        application_version="1.0.0",
        alembic_revision="0008",
        database_identity_digest="1" * 64,
        staging_batch_digest=m.checked_plan(plan, batch),
        v1_report_digest="2" * 64,
        execution_nonce="3" * 64,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=20),
    )
    registry = tmp_path / "registry"
    issue_challenge(registry, challenge)
    with store.transaction() as db:
        identity = db.scalar(select(StepRow.id).where(StepRow.run_id == run))
    target = m.request_digest(
        (batch, challenge.staging_batch_digest, ("step", identity))
    )

    def create(index=0, case="A", engine_store=None):
        return m.HostCollector(
            engine_store or store,
            plan,
            challenge,
            registry,
            tmp_path / f"host-{index}",
            str(index + 5) * 64,
            case,
            target=target,
            seconds=120,
        )

    return m, plan, challenge, registry, run, create


def observations(m, collector):
    collector.close()
    return m.read_journal(collector.root, collector.challenge, collector.registry)[1]


def accept_actual_observations(m, before, after, collectors, challenge, registry, case):
    """Independent test-only signers, including synthetic infrastructure/faults.

    This exercises saved-evidence acceptance, not physical-host certification.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from rc_acceptance import AcceptanceTrust, evidence_set_digest

    now = datetime.now(timezone.utc)
    keys, signers = {}, []
    roles = ("database", "fault", "build", "infrastructure", "reviewer", "host", "host")
    for i, role in enumerate(roles):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw().hex()
        identity = m.request_digest(public)
        keys[identity] = key
        signers.append(
            dict(
                key_id=identity,
                public_key=public,
                role=role,
                host_digest=collectors[i - 5].host if role == "host" else None,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=1),
            )
        )
    trust = AcceptanceTrust(version=1, signers=signers)
    evidence_type = m.CausalSignedEvidence.model_fields["evidence"].annotation
    from rc_causal import signing_bytes

    def sign(payload, role, start, end=None, index=0):
        signer = [s for s in signers if s["role"] == role][index]
        data = dict(
            evidence_version=3,
            challenge=challenge.model_dump(mode="json"),
            attestation_identity=signer["key_id"],
            started_at=start,
            completed_at=end or start,
            payload=payload,
        )
        evidence = evidence_type.model_validate(data)
        return m.CausalSignedEvidence.model_validate(
            dict(
                evidence=evidence.model_dump(mode="json"),
                evidence_digest=m.request_digest(evidence.model_dump(mode="json")),
                signature=keys[signer["key_id"]].sign(signing_bytes(evidence)).hex(),
            )
        )

    journals = []
    for c in collectors:
        c.close()
        _, _, digest = m.read_journal(c.root, challenge, registry)
        journals.append((c.host, digest))
    binding = m.ProvenanceBinding(
        challenge_digest=m.request_digest(challenge.model_dump(mode="json")),
        case_id=case,
        journals=journals,
        fault_record_digest="f" * 64,
    )
    sources = [
        sign(
            dict(
                source="database",
                case_id=case,
                phase="before",
                snapshot=before.model_dump(mode="json"),
            ),
            "database",
            before.observed_at,
        ),
        sign(
            dict(
                source="database",
                case_id=case,
                phase="after",
                snapshot=after.model_dump(mode="json"),
            ),
            "database",
            after.observed_at,
        ),
        sign(
            dict(
                source="fault",
                case_id=case,
                phase="lease_competition"
                if case == "A"
                else "shared_admission_competition",
                target_step_refs=[s.ref for s in after.steps],
                provider_ref=after.providers[0].ref if case == "F" else None,
                competition_seconds=5.0,
                protected_record_digest=m.request_digest(
                    binding.model_dump(mode="json")
                ),
            ),
            "fault",
            before.observed_at,
            after.observed_at,
        ),
    ]
    receipts = [
        *sources,
        sign(
            dict(
                source="build",
                identity=dict(
                    git_commit_sha=challenge.deployment_sha,
                    application_version="1.0.0",
                    runtime_digest="1" * 64,
                    admission_digest="2" * 64,
                    usage_mapping_digest="3" * 64,
                    tool_digest="4" * 64,
                ),
            ),
            "build",
            before.observed_at,
        ),
        sign(
            dict(
                source="infrastructure",
                inventory_digest="1" * 64,
                host_failure_domains=[
                    (c.host, str(i + 1) * 64) for i, c in enumerate(collectors)
                ],
            ),
            "infrastructure",
            before.observed_at,
        ),
    ]
    for index, c in enumerate(collectors):
        draft = m.unsigned_draft(c.root, challenge, registry, trust, sources, binding)
        assert "signature" not in draft and "attestation_identity" not in draft
        with pytest.raises(ValueError):
            m.CausalSignedEvidence.model_validate(draft)
        receipts.append(
            sign(
                draft["payload"],
                "host",
                draft["started_at"],
                draft["completed_at"],
                index=index,
            )
        )
    receipts.append(
        sign(
            dict(
                source="reviewer",
                case_id=case,
                review_status="approved",
                reviewed_evidence_digest=evidence_set_digest(receipts),
            ),
            "reviewer",
            datetime.now(timezone.utc),
        )
    )
    return next(
        c
        for c in m.verify_causal_acceptance(challenge, trust, receipts).cases
        if c.case_id == case
    )


def test_real_claim_and_rejection_are_redacted_and_lease_bound(store, witness):
    m, plan, challenge, registry, run, create = witness
    first, second = create(), create(1)
    before = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    task = m.ObservedStore(store, first).claim(30)
    assert task is not None and task.run_id == run
    assert m.ObservedStore(store, second).claim(30) is None
    accepted = observations(m, first)[0]
    rejected = observations(m, second)[0]
    assert (
        accepted.operation.outcome == "accepted"
        and rejected.operation.outcome == "rejected"
    )
    assert accepted.lease_digest == rejected.lease_digest
    assert accepted.target_step_ref == rejected.target_step_ref
    assert task.token not in json.dumps(
        [e.model_dump(mode="json") for e in (accepted, rejected)]
    )
    after = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    result = accept_actual_observations(
        m, before, after, [first, second], challenge, registry, "A"
    )
    assert result.status == (
        "PASS" if store.engine.dialect.name == "postgresql" else "NOT_VERIFIED"
    )


def test_independent_claim_sessions_have_only_one_owner(store, witness):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL independent transaction competition")
    m, _, _, _, _, create = witness
    other = Store(store.engine.url.render_as_string(hide_password=False))
    one, two = create(), create(1, engine_store=other)
    barrier = Barrier(2)

    def claim(owner, c):
        barrier.wait(timeout=5)
        return m.ObservedStore(owner, c).claim(30)

    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(claim, store, one), pool.submit(claim, other, two)]
            values = [f.result(timeout=15) for f in futures]
        assert sum(t is not None for t in values) == 1
        # SKIP LOCKED can return before the winner commits. That observation
        # cannot prove a target lease; a later real claim can observe it.
        loser = 0 if values[0] is None else 1
        assert (
            m.ObservedStore((store, other)[loser], (one, two)[loser]).claim(30) is None
        )
        records = observations(m, one) + observations(m, two)
        assert {r.operation.outcome for r in records} == {"accepted", "rejected"}
        assert len({r.lease_digest for r in records}) == 1
    finally:
        other.close()


def test_safe_stop_and_recovery_are_separate_actual_boundaries(store, witness):
    m, _, _, _, _, create = witness
    collector = create(case="J")
    m.ObservedStore(store, collector).claim(30)
    collector.unavailable()
    collector.stopped()
    collector.recovered()
    recorded = observations(m, collector)
    assert [(e.source, e.operation.outcome) for e in recorded[-3:]] == [
        ("database.error", "unavailable"),
        ("worker.stop", "safe_stopped"),
        ("database.probe", "accepted"),
    ]


def test_journal_scope_and_collector_identity_cannot_drift(store, witness):
    m, _, challenge, registry, _, create = witness
    c = create()
    c.close()
    header = json.loads((c.root / "header.json").read_text("utf-8"))
    header["environment_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    (c.root / "header.json").write_text(json.dumps(header), encoding="utf-8")
    with pytest.raises(ValueError, match="WitnessScopeMismatch"):
        m.read_journal(c.root, challenge, registry)


def test_clock_jump_or_event_limit_cannot_be_sealed_as_complete(
    store, witness, monkeypatch
):
    m, _, _, _, _, create = witness
    c = create()
    monkeypatch.setattr(m.time, "time", lambda: c.calibration[0] + 100)
    c.best_effort(c.unavailable)
    assert c.broken
    c.close()
    with pytest.raises(ValueError, match="WitnessIncomplete"):
        m.read_journal(c.root, c.challenge, c.registry)


def test_owned_child_exit_is_observed_before_lease_recovery(
    store, witness, monkeypatch
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("owned OS process uses a dedicated PostgreSQL database")
    m, plan, challenge, registry, run, create = witness
    parent = create(case="B")
    config = registry.parent / "child-config.json"
    nonce = "7" * 32
    child_root = registry.parent / "child"
    config.write_text(
        json.dumps(
            dict(
                plan=plan,
                challenge=challenge.model_dump(mode="json"),
                registry=str(registry),
                root=str(child_root),
            )
        ),
        encoding="utf-8",
    )
    source = """
import asyncio, json, os
from pathlib import Path
from rc_host_witness import HostCollector, CausalChallenge, ObservedStore, ObservedAgents
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.infrastructure.models.mock import MockProvider
data=json.loads(Path(os.environ['CHILD_CONFIG']).read_text('utf-8'))
store=Store(os.environ['AGENTFLOW_TEST_POSTGRES_URL'])
c=HostCollector(store, data['plan'], CausalChallenge.model_validate(data['challenge']), data['registry'], data['root'], '5'*64, 'B', seconds=60, process_nonce='7'*32)
agents=ObservedAgents(SQLAgentStore(store), c)
handlers=HandlerRegistry()
async def handler(task):
    tools=ToolRuntime()
    async def plain(args): return {}
    async def tool(args, invocation):
        await asyncio.to_thread(c.best_effort, c.dispatched, task, invocation)
        await asyncio.Event().wait()
    tools.register(Tool('echo', 'public fixture', {'type':'object'}, plain, side_effect=ToolSideEffect.READ_ONLY, invoke_durable=tool))
    proposal={'role':'assistant', 'tool_calls':[{'id':'public-call', 'type':'function', 'function':{'name':'echo','arguments':'{}'}}]}
    return await DurableAgentRunner(agents, MockProvider([proposal]), tools).run(task, 'public fixture', allowed={'echo'})
handlers.register('agent', handler)
asyncio.run(Worker(ObservedStore(store,c),handlers,lease_seconds=30).tick())
"""
    child = subprocess.Popen(
        [sys._base_executable, "-c", source],
        env={
            **os.environ,
            "CHILD_CONFIG": str(config),
            "AGENTFLOW_TEST_POSTGRES_URL": store.engine.url.render_as_string(
                hide_password=False
            ),
            "PYTHONPATH": os.pathsep.join([str(Path(m.__file__).parent), *sys.path]),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observed = Event()
    actual = parent.child_dispatch

    def watch(*args):
        result = actual(*args)
        if result is not None:
            observed.set()
        return result

    monkeypatch.setattr(parent, "child_dispatch", watch)

    def kill_owned():
        assert observed.wait(timeout=20), "ChildDispatchNotObserved"
        child.kill()  # Only this test's child on its own temporary database.

    try:
        with ThreadPoolExecutor(1) as pool:
            killer = pool.submit(kill_owned)
            parent.supervise(child, child_root, nonce)
            killer.result(timeout=25)
        assert child.returncode is not None
        recorded = observations(m, parent)
        exit_event = next(e for e in recorded if e.source == "supervisor.exit")
        assert exit_event.action_ref and exit_event.lease_digest
        assert exit_event.source_record_digest
        from sqlalchemy import update

        with store.transaction() as db:
            db.execute(
                update(StepRow)
                .where(StepRow.run_id == run)
                .values(lease_until=store._now(db) - 1)
            )
        assert store.claim() is None
        with store.transaction() as db:
            row = db.scalar(select(AgentSessionRow))
            assert row.snapshot["status"] == "waiting_reconciliation"
            assert row.snapshot["actions"][0]["status"] == "unknown"
            assert len(db.scalars(select(ArtifactRow)).all()) == 0
        # The killed child's missing seal stays incomplete.
        with pytest.raises(FileNotFoundError):
            m.read_journal(child_root, challenge, registry)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", (False, True))
@pytest.mark.parametrize("protected", (False, True))
async def test_real_worker_dispatch_and_unknown_remain_authoritative(
    store, witness, unknown, protected
):
    m, _, _, _, run, create = witness
    collector = create(case="B")
    agents = m.ObservedAgents(SQLAgentStore(store), collector)
    calls = []
    runtime = ToolRuntime()

    async def echo(args):
        return {"PRIVATE_TOOL_RESULT": 1}

    async def durable(args, invocation):
        await asyncio.to_thread(
            collector.best_effort, collector.dispatched, current[-1], invocation
        )
        calls.append(invocation.action_id)
        if unknown:
            raise TimeoutError("PRIVATE_EXCEPTION")
        return {"PRIVATE_TOOL_RESULT": 1}

    runtime.register(
        Tool(
            "echo",
            "fixed public fixture",
            {"type": "object"},
            echo,
            side_effect=ToolSideEffect.READ_ONLY,
            requires_approval=protected,
            invoke_durable=durable,
        )
    )
    current = []
    provider = MockProvider([proposal(), final()])
    handlers = HandlerRegistry()

    async def handler(task):
        current.append(task)
        return await DurableAgentRunner(agents, provider, runtime).run(
            task, "PRIVATE_PROMPT", allowed={"echo"}
        )

    handlers.register("agent", handler)
    worker = Worker(m.ObservedStore(store, collector), handlers, lease_seconds=30)
    assert await worker.tick()
    if protected:
        assert not calls
        from agentflow.domain.agent.review import ApprovalDecision

        pending = agents.list_pending_action_approvals()[0]
        agents.decide_action(
            pending.approval_id,
            ApprovalDecision(
                expected_revision=agents.review_history(pending.session_id)["revision"],
                decision="approved",
                actor="PRIVATE_ACTOR",
            ),
        )
        assert await worker.tick()
    assert len(calls) == 1
    with store.transaction() as db:
        row = db.scalar(select(AgentSessionRow))
        assert row.snapshot["status"] == (
            "waiting_reconciliation" if unknown else "succeeded"
        )
        assert len(db.scalars(select(ArtifactRow)).all()) == (0 if unknown else 1)
    if unknown:
        from agentflow.domain.agent.review import ReconciliationDecision

        pending = agents.list_unknown_actions()[0]
        agents.reconcile_action(
            pending["session_id"],
            pending["action_id"],
            ReconciliationDecision(
                request_id="synthetic-reconcile",
                expected_revision=agents.review_history(pending["session_id"])[
                    "revision"
                ],
                confirmed_status="succeeded",
                result={"PRIVATE_EVIDENCE": 1},
                evidence_reference="PRIVATE_OPERATOR_REFERENCE",
                actor="PRIVATE_ACTOR",
            ),
        )
        assert await worker.tick()
        assert len(calls) == 1 and store.get_run(run)["status"] == "succeeded"
    recorded = observations(m, collector)
    assert any(
        e.source == "tool.entry" and e.operation.outcome == "accepted" for e in recorded
    )
    if unknown:
        assert any(
            e.source == "agent.confirmed"
            and e.operation.kind == "dispatch"
            and e.operation.outcome == "unavailable"
            for e in recorded
        )
        assert any(e.reconciliation_ref for e in recorded)
    text = "".join(p.read_text("utf-8") for p in collector.root.glob("*.json"))
    assert "PRIVATE" not in text and current[0].token not in text and run not in text


def test_database_failure_does_not_become_lost_lease_or_claim_rejection(
    store, witness, monkeypatch
):
    m, _, _, _, _, create = witness
    collector = create()

    def lost(*args):
        raise AgentStoreUnavailable()

    agents = m.ObservedAgents(SQLAgentStore(store), collector)
    task = m.ObservedStore(store, collector).claim(30)
    monkeypatch.setattr(agents.delegate, "load", lost)
    with pytest.raises(AgentStoreUnavailable):
        agents.load(task)
    recorded = observations(m, collector)
    errors = [e for e in recorded if e.source == "database.error"]
    assert len(errors) == 1 and errors[0].operation.outcome == "unavailable"
    assert errors[0].operation.error_code == "DatabaseUnavailable"
    assert not any(e.operation.error_code == "LostLease" for e in recorded)


@pytest.mark.asyncio
async def test_filesystem_failure_does_not_change_committed_result(
    store, witness, monkeypatch
):
    m, _, _, _, run, create = witness
    collector = create()
    monkeypatch.setattr(
        m, "_write", lambda *a: (_ for _ in ()).throw(OSError("PRIVATE_DISK_ERROR"))
    )
    agents = m.ObservedAgents(SQLAgentStore(store), collector)
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            agents, MockProvider([final()]), ToolRuntime()
        ).run(task, "fixed public goal", allowed=set())

    handlers.register("agent", handler)
    assert await Worker(
        m.ObservedStore(store, collector), handlers, lease_seconds=30
    ).tick()
    assert store.get_run(run)["status"] == "succeeded"
    assert collector.broken
    collector.close()
    with pytest.raises((ValueError, FileNotFoundError)):
        m.read_journal(collector.root, collector.challenge, collector.registry)


def test_cancelled_old_task_really_receives_lost_lease(store, witness):
    m, _, _, _, run, create = witness
    collector = create(case="I")
    task = m.ObservedStore(store, collector).claim(30)
    from agentflow.application.ports.agent_store import AgentTransition
    from agentflow.domain.agent.budget import AgentBudget
    from agentflow.domain.agent.run import AgentSnapshot

    agents = m.ObservedAgents(SQLAgentStore(store), collector)
    state = agents.open(
        task,
        AgentSnapshot(
            id="cancel-probe",
            budget=AgentBudget(
                max_model_rounds=3,
                max_model_attempts=3,
                max_tool_calls=3,
                max_resource_units=10000,
            ),
        ),
    )
    candidate = AgentSnapshot.model_validate(
        {
            **state.snapshot.model_dump(),
            "status": "failed",
            "revision": state.snapshot.revision + 1,
        }
    )
    store.cancel(run)
    with pytest.raises(LostLease):
        agents.load(task)
    with pytest.raises(LostLease):
        agents.fail_agent_step(
            task, AgentTransition(snapshot=candidate, error_code="RCProbe")
        )
    recorded = observations(m, collector)
    rejected = next(e for e in recorded if e.operation.kind == "cancelled_commit")
    assert (
        rejected.operation.kind == "cancelled_commit"
        and rejected.operation.error_code == "LostLease"
    )
    assert store.get_run(run)["status"] == "cancelled"
    assert any(
        e.operation.kind == "resume" and e.operation.outcome == "rejected"
        for e in recorded
    )


def test_journal_missing_truncated_or_reordered_never_becomes_evidence(store, witness):
    m, _, _, _, _, create = witness
    collector = create()
    m.ObservedStore(store, collector).claim(30)
    collector.close()
    event = collector.root / "event-0000.json"
    event.write_text('{"PRIVATE":"', encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        m.read_journal(collector.root, collector.challenge, collector.registry)
    with store.transaction() as db:
        assert db.scalar(select(StepRow)).status == "running"


def test_admission_records_real_permit_and_durable_deferral(store, witness):
    m, plan, old, old_registry, _, _ = witness
    from test_provider_admission import policy, reservation

    from agentflow.application.agents.durable_runner import DurableAgentConfig
    from agentflow.domain.agent.run import AgentSnapshot

    second = store.submit(
        store.register(
            WorkflowSpec(
                name="rc-final-second", steps=(StepSpec(id="work", handler="agent"),)
            )
        ),
        {"scenario": "final", "rc_staging_batch": plan["batch_id"]},
    )
    # A new immutable plan/challenge is required; never mutate an issued batch.
    from rc_acceptance import issue_challenge

    plan = {**plan, "runs": [*plan["runs"], {"id": second, "scenario": "final"}]}
    challenge = m.CausalChallenge.model_validate(
        {
            **old.model_dump(),
            "staging_batch_digest": m.checked_plan(plan, old.environment_id),
            "execution_nonce": "4" * 64,
        }
    )
    private = old_registry.parent
    issue_challenge(private / "registry-f", challenge)
    collectors = [
        m.HostCollector(
            store,
            plan,
            challenge,
            private / "registry-f",
            private / f"f-{i}",
            str(i + 5) * 64,
            "F",
            seconds=120,
        )
        for i in range(2)
    ]
    before = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    raw = SQLAgentStore(store, admission_policy=policy())
    for i, c in enumerate(collectors):
        task = m.ObservedStore(store, c).claim(30)
        assert task is not None
        state = raw.open(
            task,
            AgentSnapshot(
                id=f"f-agent-{i}", budget=DurableAgentConfig().initial_budget()
            ),
        )
        answer = m.ObservedAgents(raw, c).reserve_model_attempt(
            task, reservation(state)
        )
        assert answer.status == ("reserved" if i == 0 else "admission_deferred")
    one, two = [observations(m, c) for c in collectors]
    accepted = next(e for e in one if e.source == "agent.reservation")
    deferred = next(e for e in two if e.source == "agent.admission_deferred")
    assert accepted.attempt_ref and accepted.permit_ref
    assert accepted.provider_ref == deferred.provider_ref
    assert deferred.operation.outcome == "unavailable" and deferred.attempt_ref is None
    after = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    result = accept_actual_observations(
        m, before, after, collectors, challenge, private / "registry-f", "F"
    )
    assert result.status == (
        "PASS" if store.engine.dialect.name == "postgresql" else "NOT_VERIFIED"
    ), result.model_dump()
    with store.transaction() as db:
        rows = db.scalars(select(AgentSessionRow).order_by(AgentSessionRow.id)).all()
        assert sorted(r.snapshot["budget"]["model_attempts"] for r in rows) == [0, 1]


def test_commit_confirmation_loss_is_not_reported_as_a_confirmed_reservation(
    store, witness, monkeypatch
):
    m, _, _, _, _, create = witness
    from test_provider_admission import policy, reservation

    from agentflow.application.agents.durable_runner import DurableAgentConfig
    from agentflow.domain.agent.run import AgentSnapshot

    collector = create(case="F")
    task = m.ObservedStore(store, collector).claim(30)
    raw = SQLAgentStore(store, admission_policy=policy())
    state = raw.open(
        task, AgentSnapshot(id="lost-ack", budget=DurableAgentConfig().initial_budget())
    )
    actual = raw.reserve_model_attempt

    def lost(*args):
        actual(*args)
        raise AgentStoreUnavailable()

    monkeypatch.setattr(raw, "reserve_model_attempt", lost)
    observed = m.ObservedAgents(raw, collector)
    with pytest.raises(AgentStoreUnavailable):
        observed.reserve_model_attempt(task, reservation(state))
    restored = observed.load(task)
    assert len(restored.model_attempts) == 1
    records = observations(m, collector)
    assert any(e.source == "database.error" for e in records)
    assert not any(e.source == "agent.reservation" for e in records)


def test_forged_claim_and_unobserved_lost_lease_do_not_produce_proof(store, witness):
    from dataclasses import replace

    m, _, _, _, _, create = witness
    c = create()
    real = store.claim(30)
    forged = replace(real, token="PRIVATE_FORGED_TOKEN")
    with pytest.raises(ValueError, match="WitnessCommitUnconfirmed"):
        c.claimed(forged)
    with pytest.raises(ValueError, match="WitnessTaskUnobserved"):
        c.rejected(forged, True)
    assert c.count == 0
    with store.transaction() as db:
        assert db.scalar(select(StepRow)).token == real.token
    observations(m, c)


def test_forged_lost_lease_with_current_ownership_is_not_a_receipt(
    store, witness, monkeypatch
):
    from agentflow.application.agents.durable_runner import (
        DurableAgentConfig,
        successor,
    )
    from agentflow.application.ports.agent_store import AgentTransition
    from agentflow.domain.agent.run import AgentSnapshot
    from agentflow.domain.agent.status import AgentStatus

    m, _, _, _, _, create = witness
    c = create()
    task = m.ObservedStore(store, c).claim(30)
    raw = SQLAgentStore(store)
    state = raw.open(
        task,
        AgentSnapshot(id="valid-owner", budget=DurableAgentConfig().initial_budget()),
    )

    def forged(*args):
        raise LostLease()

    monkeypatch.setattr(raw, "fail_agent_step", forged)
    with pytest.raises(LostLease):
        m.ObservedAgents(raw, c).fail_agent_step(
            task,
            AgentTransition(
                snapshot=successor(state.snapshot, status=AgentStatus.FAILED),
                error_code="RCProbe",
            ),
        )
    assert c.broken
    assert c.count == 1
    assert store.heartbeat(task, 30)
    c.close()


def test_consumption_during_collection_refuses_the_next_real_claim(store, witness):
    from rc_acceptance import consume_challenge

    m, _, challenge, registry, _, create = witness
    c = create()
    consume_challenge(registry, challenge)
    with pytest.raises(ValueError, match="ChallengeReplay"):
        m.ObservedStore(store, c).claim(30)
    with store.transaction() as db:
        row = db.scalar(select(StepRow))
        assert row.status == "ready" and row.attempts == 0
    c.close()


def test_exclusive_files_and_event_cap_preserve_runtime_state(store, witness):
    m, _, _, _, _, create = witness
    c = create()
    header = (c.root / "header.json").read_bytes()
    with pytest.raises(FileExistsError):
        m._write(c.root / "header.json", {})
    assert (c.root / "header.json").read_bytes() == header
    c.count = m.MAX_EVENTS  # Exercise the collector bound, not Runtime data.
    task = store.claim(30)
    c.best_effort(c.claimed, task)
    assert c.broken
    assert store.heartbeat(task, 30)
    c.close()
    with pytest.raises(ValueError, match="WitnessIncomplete"):
        m.read_journal(c.root, c.challenge, c.registry)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", (False, True))
async def test_model_unknown_and_confirmed_backpressure_remain_distinct(
    store, witness, unknown
):
    from test_provider_admission import policy

    from agentflow.application.ports.model_provider import (
        ModelOutcomeUnknown,
        ModelRateLimitedError,
    )

    m, _, _, _, _, create = witness
    c = create(case="G")
    agents = m.ObservedAgents(SQLAgentStore(store, admission_policy=policy()), c)
    provider = MockProvider(
        [
            ModelOutcomeUnknown()
            if unknown
            else ModelRateLimitedError(retry_after_seconds=5)
        ]
    )
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            agents, provider, ToolRuntime(), admission=agents
        ).run(task, "public fixture", allowed=set())

    handlers.register("agent", handler)
    assert await Worker(m.ObservedStore(store, c), handlers, lease_seconds=30).tick()
    if not unknown:
        assert m.ObservedStore(store, c).claim(30) is None
    records = observations(m, c)
    if unknown:
        assert any(
            e.source == "agent.confirmed"
            and e.operation.outcome == "unavailable"
            and e.attempt_ref
            for e in records
        )
        assert not any(e.operation.kind == "rate_limited" for e in records)
    else:
        limited = next(e for e in records if e.source == "database.backpressure")
        assert limited.attempt_ref and limited.model_call_ref and limited.provider_ref
    with store.transaction() as db:
        row = db.scalar(select(AgentSessionRow))
        assert row.snapshot["budget"]["model_attempts"] == 1
        assert len(db.scalars(select(ArtifactRow)).all()) == 0
