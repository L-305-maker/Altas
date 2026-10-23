"""Mandatory offline release checks with complete synthetic A-J ceremonies."""

import json
from datetime import timedelta

import pytest
from rc_release_fixtures import ReleaseBundle
from test_rc_causal import acceptance as acceptance
from test_rc_causal import causal as causal
from test_rc_tools import script


@pytest.fixture
def release_bundle(monkeypatch, causal, tmp_path):
    c, old_challenge, _, _, bundle, _ = causal
    m = script(monkeypatch, "rc_host_witness")
    script(monkeypatch, "rc_witness_provenance")
    fixture = ReleaseBundle(tmp_path)
    fixture.base = old_challenge.issued_at + timedelta(seconds=1)
    for n, case in enumerate("ABCDEFGHIJ"):

        def rebase(data, case=case, n=n):
            def refs(value):
                if isinstance(value, dict):
                    return {k: refs(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [refs(v) for v in value]
                if (
                    isinstance(value, str)
                    and len(value) == 64
                    and value
                    not in {"5" * 64, "6" * 64, old_challenge.database_identity_digest}
                ):
                    return m.request_digest((case, value))
                return value

            changed = refs(data)
            run_ref = m.request_digest(
                (
                    str(fixture.challenge.environment_id),
                    fixture.challenge.staging_batch_digest,
                    ("run", fixture.plan["runs"][n]["id"]),
                )
            )
            for phase in ("before", "after"):
                changed[phase]["observed_at"] = fixture.base + timedelta(
                    seconds=0 if phase == "before" else 25
                )
                for step in changed[phase]["steps"]:
                    step["run_ref"] = run_ref
            # Original fixtures use their own recent base clock. Preserve all
            # causal differences while shifting the full ceremony together.
            offset = (
                fixture.base - old_challenge.issued_at - timedelta(seconds=1)
            ).total_seconds()

            def clock(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if (
                            key
                            in {
                                "at",
                                "lease_until",
                                "created_at",
                                "available_at",
                                "cooldown_until",
                                "consumed_at",
                                "reconciled_at",
                                "confirmed_at",
                                "expires_at",
                                "released_at",
                            }
                            and isinstance(item, (int, float))
                            and item > 1_000_000_000
                        ):
                            value[key] = item + offset
                        else:
                            clock(item)
                elif isinstance(value, list):
                    for item in value:
                        clock(item)

            clock(changed)
            data.update(changed)

        receipts = bundle(case, rebase, review=False)
        before, after = [
            next(
                r.evidence.payload.snapshot
                for r in receipts
                if r.evidence.payload.source == "database"
                and r.evidence.payload.phase == phase
            )
            for phase in ("before", "after")
        ]
        hosts = [
            r.evidence.payload for r in receipts if r.evidence.payload.source == "host"
        ]
        entries = []
        for i, host in enumerate(hosts):
            values = []
            for op in host.operations:
                operation = op.model_dump(
                    mode="json", include=set(m.Operation.model_fields)
                )
                deferred = operation["error_code"] == "ProviderAdmissionDeferred"
                if deferred:
                    operation["error_code"] = None
                source = (
                    "agent.admission_deferred"
                    if deferred
                    else {
                        "claim": "store.claim",
                        "worker_exit": "supervisor.exit",
                        "stale_commit": "agent.lost_lease",
                        "cancelled_commit": "agent.lost_lease",
                        "resume": "agent.load",
                        "model_reserve": "agent.reservation",
                        "rate_limited": "database.backpressure",
                        "database_unavailable": "worker.stop",
                        "database_recovered": "database.probe",
                        "dispatch": "tool.entry",
                    }[op.kind]
                )
                fields = op.model_dump(
                    mode="json", include=set(m.Observation.model_fields) - {"operation"}
                )
                if case == "J":
                    operation["target"] = fixture.challenge.database_identity_digest
                invocation = None
                if case == "A":
                    invocation = dict(
                        invocation_id=m.request_digest((case, i))[:32],
                        started_at=fixture.base.timestamp() + (4 if i == 0 else 5.25),
                        completed_at=fixture.base.timestamp() + (7 if i == 0 else 5.75),
                        clock_error_seconds=0.01,
                        clock_source="postgresql",
                        outcome="task" if i == 0 else "none",
                    )
                values.append(
                    m.Observation(
                        operation=operation,
                        source=source,
                        clock_error_seconds=0.0,
                        run_ref=before.steps[0].run_ref,
                        invocation=invocation,
                        **fields,
                    )
                )
            entries.append(values)
        fixture.add_case(case, before, after, entries)
    return fixture


def gate(monkeypatch):
    return script(monkeypatch, "verify_rc_release")


def test_complete_bundle_has_strict_offline_pass_but_no_real_environment_promotion(
    monkeypatch, release_bundle
):
    f = release_bundle
    assert {
        c.status
        for c in f.m.verify_causal_acceptance(f.challenge, f.trust, f.receipts()).cases
    } == {"PASS"}
    result = gate(monkeypatch).evaluate(f.args)
    assert result.saved_evidence_validation == "PASS"
    assert {c.status for c in result.cases} == {"PASS"}
    assert (
        result.provenance_validation
        == result.signed_evidence_validation
        == result.independent_review
        == "PASS"
    )
    assert result.status == "NOT_VERIFIED"
    for field in (
        "transaction_contention",
        "physical_hosts",
        "cross_host_recovery",
        "provider_acceptance",
        "token_conformance",
        "production_release",
    ):
        assert getattr(result, field) == "NOT_VERIFIED"
    assert not (f.registry / "consumed.json").exists()
    assert result.model_dump(mode="json") == gate(monkeypatch).evaluate(
        f.args
    ).model_dump(mode="json")


@pytest.mark.parametrize("kind", ("sequential", "uncertain", "database_only"))
def test_old_v3_pass_never_bypasses_mandatory_contention_gate(
    monkeypatch, release_bundle, kind
):
    f = release_bundle
    if kind == "sequential":
        f.change_journal(
            "A",
            0,
            lambda h, o: o["invocation"].update(completed_at=f.base.timestamp() + 5.1),
        )
    elif kind == "uncertain":
        f.change_journal(
            "A",
            0,
            lambda h, o: o["invocation"].update(
                completed_at=f.base.timestamp() + 5.3, clock_error_seconds=0.2
            ),
        )
    else:

        def unavailable(header, observation):
            observation["operation"].update(
                outcome="unavailable", error_code="DatabaseUnavailable"
            )
            observation.update(source="database.error")
            observation["invocation"]["outcome"] = "database_error"

        f.change_journal("A", 1, unavailable)
    f.rebind("A")
    legacy = f.m.verify_causal_acceptance(f.challenge, f.trust, f.receipts()).cases[0]
    assert legacy.status == ("NOT_VERIFIED" if kind == "database_only" else "PASS")
    result = gate(monkeypatch).evaluate(f.args)
    assert result.cases[0].status == "NOT_VERIFIED"
    assert (
        result.cases[0].claim_invocations
        == {
            "sequential": "SEQUENTIAL_VALID_LEASE",
            "uncertain": "TIMING_UNCERTAIN",
            "database_only": "NO_CONTENTION_PROOF",
        }[kind]
    )
    assert result.saved_evidence_validation != "PASS"
    assert result.physical_hosts == result.transaction_contention == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "field",
    (
        "at",
        "target",
        "target_step_ref",
        "lease_digest",
        "action_ref",
        "attempt_ref",
        "envelope",
    ),
)
def test_valid_signature_cannot_replace_exact_journal_comparison(
    monkeypatch, release_bundle, field
):
    f = release_bundle

    def change(value):
        if field == "envelope":
            value["started_at"] = f.base.isoformat()
            return
        op = value["payload"]["operations"][0]
        op[field] = op[field] + 0.1 if field == "at" else "d" * 64
        if field == "at":
            value["completed_at"] = (f.base + timedelta(seconds=10)).isoformat()
        op["witness_digest"] = f.m.request_digest(
            {k: v for k, v in op.items() if k != "witness_digest"}
        )

    f.change_host("A", 0, change)
    result = gate(monkeypatch).evaluate(f.args)
    case = result.cases[0]
    assert case.status == "FAIL"
    assert "HostJournalMismatch" in case.errors
    assert result.saved_evidence_validation == "FAIL"


@pytest.mark.parametrize(
    "attack",
    (
        "missing_journal",
        "missing_fault",
        "missing_reviewer",
        "consumed",
        "expired",
        "trust_pin",
        "batch",
        "collector",
        "process",
        "journal_replacement",
        "fake_bool",
        "fake_report",
        "duplicate_receipt",
        "cross_case",
        "challenge",
        "file_type",
        "oversized",
        "duplicate_fields",
    ),
)
def test_strict_gate_cannot_be_bypassed(monkeypatch, release_bundle, attack):
    f = release_bundle
    if attack == "missing_journal":
        (f.bundle / "A" / "host-1" / "closed.json").unlink()
    elif attack in {"missing_fault", "missing_reviewer"}:
        (
            f.bundle
            / "A"
            / ("fault.json" if attack == "missing_fault" else "reviewer.json")
        ).unlink()
    elif attack == "consumed":
        f.a.consume_challenge(f.registry, f.challenge)
    elif attack == "expired":
        expired = f.challenge.model_copy(
            update={"expires_at": f.challenge.issued_at + timedelta(seconds=1)}
        )
        f.write(f.registry / "issued.json", expired)
        f.write(f.args.challenge, expired)
    elif attack == "trust_pin":
        f.args.trust_sha256 = "0" * 64
    elif attack == "collector":
        f.args.collector_sha256 = "0" * 64
    elif attack in {"batch", "process", "journal_replacement", "challenge"}:
        f.change_journal(
            "A",
            1,
            lambda h, o: h.update(
                {"challenge_digest": "0" * 64}
                if attack == "challenge"
                else {"staging_batch_digest": "0" * 64}
                if attack == "batch"
                else {"process_nonce": "0" * 32}
                if attack == "process"
                else {"process_id": 999}
            ),
        )
    elif attack == "file_type":
        path = f.bundle / "build.json"
        path.unlink()
        path.mkdir()
    elif attack == "oversized":
        (f.bundle / "A" / "host-0.json").write_text(" " * 2_100_001, encoding="utf-8")
    elif attack == "duplicate_fields":
        (f.bundle / "A" / "host-0.json").write_text(
            '{"evidence":{},"evidence":{}}', encoding="utf-8"
        )
    elif attack in {"fake_bool", "fake_report"}:
        f.write(
            f.bundle / "A" / "host-1.json",
            {"eligible_for_signing": True}
            if attack == "fake_bool"
            else gate(monkeypatch).evaluate(f.args),
        )
    elif attack == "duplicate_receipt":
        (f.bundle / "A" / "host-1.json").write_bytes(
            (f.bundle / "A" / "host-0.json").read_bytes()
        )
    else:
        (f.bundle / "A" / "host-1.json").write_bytes(
            (f.bundle / "B" / "host-1.json").read_bytes()
        )
    module = gate(monkeypatch)
    code, result = module.run(f.args)
    assert code in {1, 2}
    assert result.status != "PASS" and result.saved_evidence_validation != "PASS"


@pytest.mark.parametrize("case", tuple("ABCDEFGHIJ"))
def test_every_case_requires_exact_signed_journal_payload(
    monkeypatch, release_bundle, case
):
    f = release_bundle
    f.change_host(case, 0, lambda d: d.update(started_at=f.base.isoformat()))
    legacy = f.m.verify_causal_acceptance(f.challenge, f.trust, f.receipts())
    assert next(c for c in legacy.cases if c.case_id == case).status == "PASS"
    result = gate(monkeypatch).evaluate(f.args)
    checked = next(c for c in result.cases if c.case_id == case)
    assert checked.status == "FAIL" and "HostJournalMismatch" in checked.errors


@pytest.mark.parametrize(
    "case,field",
    (
        ("B", "action_ref"),
        ("C", "lease_digest"),
        ("D", "approval_ref"),
        ("E", "reconciliation_ref"),
        ("F", "provider_ref"),
        ("G", "model_call_ref"),
        ("H", "target_step_ref"),
        ("I", "error_code"),
        ("J", "outcome"),
    ),
)
def test_matching_signatures_and_journals_still_require_each_causal_scenario(
    monkeypatch, release_bundle, case, field
):
    f = release_bundle
    index = 1 if case == "F" else 0

    def change(header, observation):
        if field == "error_code":
            observation["operation"][field] = "DatabaseUnavailable"
        elif field == "outcome":
            observation["operation"][field] = "unavailable"
        else:
            observation[field] = "d" * 64

    f.change_journal(case, index, change)
    f.rebind(case)
    result = gate(monkeypatch).evaluate(f.args)
    checked = next(c for c in result.cases if c.case_id == case)
    assert checked.status != "PASS"
    if case in {"D", "E"}:
        assert checked.causal_acceptance == "PASS"  # Preserve the historical result.
        assert checked.provenance_validation == "FAIL"
        assert "OperationReferenceMismatch" in checked.errors
    else:
        assert checked.causal_acceptance != "PASS"
    assert result.saved_evidence_validation != "PASS"


def test_verify_recomputes_and_consumes_once(monkeypatch, release_bundle):
    f = release_bundle
    module = gate(monkeypatch)
    assert module.run(f.args)[0] == 0
    assert not (f.registry / "consumed.json").exists()
    # Change a signed object after precheck, with a legitimate new signature
    # and refreshed Reviewer; the final call must recompute the comparison.
    f.change_host("A", 0, lambda d: d.update(started_at=f.base.isoformat()))
    f.args.command = "verify"
    code, report = module.run(f.args)
    assert code == 2 and report.saved_evidence_validation == "FAIL"
    assert (f.registry / "consumed.json").exists()
    assert module.run(f.args)[0] == 2


def test_invalid_signature_does_not_consume_challenge(monkeypatch, release_bundle):
    f = release_bundle
    path = f.bundle / "A" / "host-0.json"
    value = json.loads(path.read_text("utf-8"))
    value["signature"] = "0" * 128
    f.write(path, value)
    f.args.command = "verify"
    code, report = gate(monkeypatch).run(f.args)
    assert code == 2 and report.signed_evidence_validation == "FAIL"
    assert report.independent_review != "PASS"
    assert not (f.registry / "consumed.json").exists()


def test_final_verification_has_one_atomic_registry_winner(monkeypatch, release_bundle):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f = release_bundle
    module = gate(monkeypatch)
    f.args.command = "verify"
    barrier = Barrier(2)
    actual = module.consume_challenge

    def consume(*args):
        barrier.wait(timeout=20)
        actual(*args)

    monkeypatch.setattr(module, "consume_challenge", consume)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(module.run, f.args) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(code for code, _ in results) == [0, 2]
    assert (f.registry / "consumed.json").exists()


def test_inputs_cannot_change_challenge_after_recomputation(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = gate(monkeypatch)
    actual = module.evaluate

    def evaluate(args):
        report = actual(args)
        changed = f.challenge.model_copy(update={"execution_nonce": "0" * 64})
        f.write(f.args.challenge, changed)
        return report

    monkeypatch.setattr(module, "evaluate", evaluate)
    f.args.command = "verify"
    code, report = module.run(f.args)
    assert code == 2 and report.saved_evidence_validation == "FAIL"
    assert not (f.registry / "consumed.json").exists()


def test_cli_is_offline_and_redacted(monkeypatch, release_bundle, capsys):
    f = release_bundle
    module = gate(monkeypatch)
    import rc_staging

    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **k: pytest.fail("OfflineReleaseConnected")
    )
    argv = ["verify_rc_release.py", "precheck"]
    for key, value in vars(f.args).items():
        if key != "command":
            argv.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr("sys.argv", argv)
    assert module.main() == 0
    text = capsys.readouterr().out
    assert json.loads(text)["production_release"] == "NOT_VERIFIED"
    assert str(f.root) not in text and "process_id" not in text
