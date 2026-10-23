"""Deterministic claim races; all writers use independent real connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import event, select
from test_rc_host_witness import (
    accept_actual_observations,
    observations,
)
from test_rc_host_witness import (
    store as store,
)
from test_rc_host_witness import (
    witness as witness,
)

from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import RunRow, StepRow
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_only", (False, True))
@pytest.mark.parametrize("journal_failed", (False, True))
async def test_insert_after_isolation_never_enters_handler(
    store, witness, monkeypatch, claim_only, journal_failed
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    m, _, _, _, run, create = witness
    collector = create()
    collector.broken = journal_failed
    other = Store(store.engine.url.render_as_string(hide_password=False))
    boundary = Barrier(2)
    original = collector.isolated
    outside = []
    effects = []

    def isolated():
        original()
        boundary.wait(timeout=10)
        boundary.wait(timeout=10)

    def insert():
        boundary.wait(timeout=10)
        foreign = other.submit(
            other.register(
                WorkflowSpec(
                    name="outside-authorized-plan",
                    steps=(StepSpec(id="work", handler="agent"),),
                )
            ),
            {"scenario": "final", "rc_staging_batch": "outside"},
        )
        # Store orders by creation time. Make the injected ready Run first,
        # deterministically, after isolation and before the actual claim.
        with other.transaction() as db:
            db.get(RunRow, foreign).created_at = 0
        outside.append(foreign)
        boundary.wait(timeout=10)

    async def handler(task):
        effects.extend(("handler", "model", "tool"))
        return {"unexpected": True}

    import rc_workload

    monkeypatch.setattr(rc_workload, "Store", lambda url: store)
    monkeypatch.setattr(rc_workload.HandlerRegistry, "get", lambda self, name: handler)
    monkeypatch.setattr(collector, "isolated", isolated)
    try:
        with ThreadPoolExecutor(1) as pool:
            writer = pool.submit(insert)
            with pytest.raises(ValueError, match="WitnessExecutionRejected"):
                await rc_workload.serve(
                    None, None, 10, witness=collector, claim_only=claim_only
                )
            writer.result(timeout=10)
        assert effects == []
        assert collector.broken
        with other.transaction() as db:
            foreign = db.scalar(select(StepRow).where(StepRow.run_id == outside[0]))
            authorized = db.scalar(select(StepRow).where(StepRow.run_id == run))
            # The global Core transaction DID commit. Do not compensate it.
            assert foreign.status == "running" and foreign.token
            assert foreign.attempts == 1 and foreign.lease_until > 0
            assert authorized.status == "ready" and authorized.attempts == 0
    finally:
        collector.close()
        other.close()


def test_sequential_claims_are_not_concurrent_provenance(store, witness):
    m, plan, challenge, registry, _, create = witness
    one, two = create(), create(1)
    before = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    assert m.ObservedStore(store, one).claim(30) is not None
    assert m.ObservedStore(store, two).claim(30) is None
    after = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    legacy = accept_actual_observations(
        m, before, after, [one, two], challenge, registry, "A"
    )
    if store.engine.dialect.name == "postgresql":
        assert legacy.status == "PASS"  # Documents the v3 temporal ambiguity.
    first, second = observations(m, one)[0], observations(m, two)[0]
    assert first.invocation.completed_at <= second.invocation.started_at
    from rc_witness_provenance import case_a_contention

    assert case_a_contention(
        [(one.header, [first], "1" * 64), (two.header, [second], "2" * 64)]
    ) == (
        "SEQUENTIAL_VALID_LEASE"
        if store.engine.dialect.name == "postgresql"
        else "NO_CONTENTION_PROOF"
    )


def test_overlapping_claims_have_one_owner_and_provable_invocations(
    store, witness, monkeypatch
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    m, plan, challenge, registry, _, create = witness
    other = Store(store.engine.url.render_as_string(hide_password=False))
    one, two = create(), create(1, engine_store=other)
    before = m.collect_snapshot(
        store.engine, plan, challenge.environment_id, evidence_version=3
    )
    boundary = Barrier(2)
    locked, committed = Event(), Event()
    actual = store.claim

    def winner_sql(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE SKIP LOCKED" in statement:
            locked.set()  # The real SELECT has acquired the Run row lock.
            boundary.wait(timeout=10)

    def loser_sql(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE SKIP LOCKED" in statement:
            boundary.wait(timeout=10)  # Both real SQL transactions overlap.
            assert committed.wait(timeout=10)

    def winner(seconds):
        task = actual(seconds)
        committed.set()
        return task

    def loser(seconds):
        assert locked.wait(timeout=10)
        return other_actual(seconds)

    other_actual = other.claim
    monkeypatch.setattr(store, "claim", winner)
    monkeypatch.setattr(other, "claim", loser)
    event.listen(store.engine, "after_cursor_execute", winner_sql)
    event.listen(other.engine, "after_cursor_execute", loser_sql)
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(m.ObservedStore(store, one).claim, 30),
                pool.submit(m.ObservedStore(other, two).claim, 30),
            ]
            results = [f.result(timeout=20) for f in futures]
        assert sum(t is not None for t in results) == 1
        from rc_witness_provenance import case_a_contention

        journals = [
            (c.header, observations(m, c), str(i + 1) * 64)
            for i, c in enumerate((one, two))
        ]
        # Even overlapping wrappers do not establish overlapping SQL locks.
        # A signed v3 operation contains no invocation interval at all.
        assert case_a_contention(journals) in {
            "OVERLAPPING_INVOCATIONS",
            "TIMING_UNCERTAIN",
        }
        after = m.collect_snapshot(
            store.engine, plan, challenge.environment_id, evidence_version=3
        )
        legacy = accept_actual_observations(
            m, before, after, [one, two], challenge, registry, "A"
        )
        assert legacy.status == "PASS"  # Same v3 result as sequential claims.
    finally:
        event.remove(store.engine, "after_cursor_execute", winner_sql)
        event.remove(other.engine, "after_cursor_execute", loser_sql)
        one.close()
        two.close()
        other.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "scenario",
        "batch",
        "handler",
        "step",
        "run",
        "token",
        "attempt",
        "persisted_spec",
        "persisted_step",
    ),
)
def test_task_authorization_cannot_drift_after_claim(store, witness, mutation):
    from dataclasses import replace

    m, plan, _, _, run, create = witness
    collector = create()
    task = store.claim(30)
    if mutation in {"scenario", "batch"}:
        inputs = {
            **task.inputs,
            "scenario" if mutation == "scenario" else "rc_staging_batch": "foreign",
        }
        task = replace(task, inputs=inputs)
    elif mutation == "handler":
        task = replace(task, spec=task.spec.model_copy(update={"handler": "rc-plain"}))
    elif mutation in {"step", "run", "token", "attempt"}:
        field = {
            "step": "step_id",
            "run": "run_id",
            "token": "token",
            "attempt": "attempt",
        }[mutation]
        task = replace(task, **{field: 99 if mutation == "attempt" else "foreign"})
    else:
        with store.transaction() as db:
            if mutation == "persisted_step":
                db.scalar(
                    select(StepRow).where(StepRow.run_id == run)
                ).id = "foreign-step-identity"
            else:
                row = db.get(RunRow, run)
                definition = {
                    **row.spec,
                    "steps": [{**row.spec["steps"][0], "handler": "rc-plain"}],
                }
                row.spec = definition
    with pytest.raises(m.WitnessExecutionRejected):
        collector.authorize(task)
    assert collector.broken
    with store.transaction() as db:
        step = db.scalar(select(StepRow).where(StepRow.run_id == run))
        assert step.status == "running" and step.attempts == 1
    # Mutating the original plan cannot change the collector's authorization.
    plan["runs"][0]["scenario"] = "normal"
    assert collector.plan["runs"][0]["scenario"] == "final"
    collector.close()


def test_claim_connection_failure_is_an_invocation_error_not_competition(
    store, witness, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    m, _, _, _, _, create = witness
    collector = create()

    def unavailable(seconds):
        raise OperationalError("redacted", {}, Exception())

    monkeypatch.setattr(store, "claim", unavailable)
    with pytest.raises(OperationalError):
        m.ObservedStore(store, collector).claim(30)
    records = observations(m, collector)
    assert len(records) == 1
    assert records[0].source == "database.error"
    assert records[0].invocation.outcome == "database_error"
    assert records[0].operation.outcome == "unavailable"
