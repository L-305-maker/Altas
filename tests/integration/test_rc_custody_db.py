"""Owned local PG evidence survives source replacement after custody freeze."""

import hashlib
from pathlib import Path

import pytest
from rc_release_fixtures import ReleaseBundle
from test_rc_host_witness import store as store
from test_rc_host_witness import witness as witness


def test_actual_claim_observations_remain_bound_to_frozen_copy(
    store, witness, tmp_path
):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("custody integration requires dedicated PostgreSQL")
    import rc_evidence_custody as custody
    import verify_rc_release as gate

    m, plan, _, _, _, _ = witness
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
            "A",
            seconds=120,
        )
        for i in range(2)
    ]
    before = m.collect_snapshot(
        store.engine, plan, f.challenge.environment_id, evidence_version=3
    )
    try:
        for c in collectors:
            c.target = before.steps[0].ref
        assert m.ObservedStore(store, collectors[0]).claim(30) is not None
        assert m.ObservedStore(store, collectors[1]).claim(30) is None
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
        private = custody.init_store(f.root / "custody")
        manifest = custody.freeze(f.args, private, stage="final")
        f.args.custody_store, f.args.manifest_sha256 = (
            str(private),
            custody.digest(manifest),
        )
        (f.bundle / "A/before.json").write_text("{}", encoding="utf-8")
        f.args.command = "custody-precheck"
        code, report = gate.run(f.args)
        assert code == 1 and report.custody_validation == "PASS"
        assert report.cases[0].claim_invocations == "SEQUENTIAL_VALID_LEASE"
        assert (
            report.saved_evidence_validation == report.physical_hosts == "NOT_VERIFIED"
        )
        assert not (f.registry / "consumed.json").exists()
        f.args.command = "custody-verify"
        assert gate.run(f.args)[0] == 1
        assert (f.registry / "consumed.json").exists()
    finally:
        for c in collectors:
            c.close()
