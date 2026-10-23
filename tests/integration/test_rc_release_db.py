"""Real local PG observations enter the mandatory offline evidence gate."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from rc_release_fixtures import ReleaseBundle
from sqlalchemy import event
from test_rc_host_witness import store as store
from test_rc_host_witness import witness as witness

from agentflow.infrastructure.persistence.store import Store


@pytest.mark.parametrize("mode", ("sequential", "overlapping", "signed_mismatch"))
def test_actual_claims_cannot_skip_strict_release_gate(
    store, witness, tmp_path, monkeypatch, mode
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("strict release invocation evidence requires PostgreSQL")
    m, plan, _, _, _, _ = witness
    f = ReleaseBundle(tmp_path / "release", plan=plan)
    f.args.collector_sha256 = hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
    other = Store(store.engine.url.render_as_string(hide_password=False))
    collectors = [
        m.HostCollector(
            owner,
            plan,
            f.challenge,
            f.registry,
            tmp_path / f"actual-{i}",
            str(i + 5) * 64,
            "A",
            seconds=120,
        )
        for i, owner in enumerate((store, other))
    ]
    before = m.collect_snapshot(
        store.engine, plan, f.challenge.environment_id, evidence_version=3
    )
    target = before.steps[0].ref
    for collector in collectors:
        collector.target = target
    locked, committed, boundary = Event(), Event(), Barrier(2)

    def winner_sql(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE SKIP LOCKED" in statement:
            locked.set()
            boundary.wait(timeout=10)

    def loser_sql(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE SKIP LOCKED" in statement:
            boundary.wait(timeout=10)
            assert committed.wait(timeout=10)

    actual, other_actual = store.claim, other.claim

    def win(seconds):
        answer = actual(seconds)
        committed.set()
        return answer

    def lose(seconds):
        assert locked.wait(timeout=10)
        return other_actual(seconds)

    try:
        if mode == "overlapping":
            event.listen(store.engine, "after_cursor_execute", winner_sql)
            event.listen(other.engine, "after_cursor_execute", loser_sql)
            monkeypatch.setattr(store, "claim", win)
            monkeypatch.setattr(other, "claim", lose)
            with ThreadPoolExecutor(2) as pool:
                futures = [
                    pool.submit(m.ObservedStore(owner, c).claim, 30)
                    for owner, c in zip((store, other), collectors, strict=True)
                ]
                tasks = [future.result(timeout=20) for future in futures]
            assert sum(t is not None for t in tasks) == 1
        else:
            assert m.ObservedStore(store, collectors[0]).claim(30) is not None
            assert m.ObservedStore(other, collectors[1]).claim(30) is None
        after = m.collect_snapshot(
            store.engine, plan, f.challenge.environment_id, evidence_version=3
        )
        entries = []
        for c in collectors:
            c.close()
            entries.append(m.read_journal(c.root, f.challenge, f.registry)[1])
        f.add_case(
            "A",
            before,
            after,
            entries,
            collector_headers=[c.header for c in collectors],
        )
        for i, c in enumerate(collectors):
            copied = m.read_journal(
                f.bundle / "A" / f"host-{i}", f.challenge, f.registry
            )
            assert copied[2] == m.read_journal(c.root, f.challenge, f.registry)[2]
        legacy = m.verify_causal_acceptance(f.challenge, f.trust, f.receipts()).cases[0]
        assert legacy.status == "PASS"
        if mode == "signed_mismatch":
            f.change_host("A", 0, lambda d: d.update(started_at=f.base.isoformat()))
        import verify_rc_release

        result = verify_rc_release.evaluate(f.args)
        checked = result.cases[0]
        if mode == "sequential":
            assert checked.status == "NOT_VERIFIED"
            assert checked.claim_invocations == "SEQUENTIAL_VALID_LEASE"
        elif mode == "signed_mismatch":
            assert checked.status == "FAIL" and "HostJournalMismatch" in checked.errors
        else:
            assert checked.claim_invocations in {
                "OVERLAPPING_INVOCATIONS",
                "TIMING_UNCERTAIN",
            }
            assert checked.status == (
                "PASS"
                if checked.claim_invocations == "OVERLAPPING_INVOCATIONS"
                else "NOT_VERIFIED"
            )
        assert result.saved_evidence_validation != "PASS"  # Other A-J cases are absent.
        assert (
            result.physical_hosts
            == result.transaction_contention
            == result.production_release
            == "NOT_VERIFIED"
        )
        assert not (f.registry / "consumed.json").exists()
    finally:
        if mode == "overlapping":
            event.remove(store.engine, "after_cursor_execute", winner_sql)
            event.remove(other.engine, "after_cursor_execute", loser_sql)
        for c in collectors:
            c.close()
        other.close()


def test_actual_shared_admission_receipts_remain_pool_and_journal_bound(
    store, witness, tmp_path
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("strict release admission evidence requires PostgreSQL")
    from test_provider_admission import policy, reservation

    from agentflow.application.agents.durable_runner import DurableAgentConfig
    from agentflow.domain.agent.run import AgentSnapshot
    from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
    from agentflow.infrastructure.persistence.agent_store import SQLAgentStore

    m, plan, _, _, _, _ = witness
    second = store.submit(
        store.register(
            WorkflowSpec(
                name="rc-final-release-peer",
                steps=(StepSpec(id="work", handler="agent"),),
            )
        ),
        {"scenario": "final", "rc_staging_batch": plan["batch_id"]},
    )
    plan = {**plan, "runs": [*plan["runs"], {"id": second, "scenario": "final"}]}
    f = ReleaseBundle(tmp_path / "release", plan=plan)
    f.args.collector_sha256 = hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
    collectors = [
        m.HostCollector(
            store,
            plan,
            f.challenge,
            f.registry,
            tmp_path / f"actual-{i}",
            str(i + 5) * 64,
            "F",
            seconds=120,
        )
        for i in range(2)
    ]
    before = m.collect_snapshot(
        store.engine, plan, f.challenge.environment_id, evidence_version=3
    )
    agents = SQLAgentStore(store, admission_policy=policy())
    for i, c in enumerate(collectors):
        task = m.ObservedStore(store, c).claim(30)
        state = agents.open(
            task,
            AgentSnapshot(
                id=f"release-admission-{i}",
                budget=DurableAgentConfig().initial_budget(),
            ),
        )
        answer = m.ObservedAgents(agents, c).reserve_model_attempt(
            task, reservation(state)
        )
        assert answer.status == ("reserved" if i == 0 else "admission_deferred")
    after = m.collect_snapshot(
        store.engine, plan, f.challenge.environment_id, evidence_version=3
    )
    assert len(after.permits) == len(after.attempts) == 1
    entries = []
    for c in collectors:
        c.close()
        entries.append(m.read_journal(c.root, f.challenge, f.registry)[1])
    f.add_case(
        "F", before, after, entries, collector_headers=[c.header for c in collectors]
    )
    import verify_rc_release

    result = verify_rc_release.evaluate(f.args)
    checked = next(c for c in result.cases if c.case_id == "F")
    assert checked.status == "PASS", checked.errors
    assert (
        checked.provenance_validation
        == checked.signed_evidence_validation
        == checked.causal_acceptance
        == checked.independent_review
        == "PASS"
    )
    assert (
        result.saved_evidence_validation == result.production_release == "NOT_VERIFIED"
    )
