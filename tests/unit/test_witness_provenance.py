"""Offline adversarial provenance; synthetic signers never certify real hosts."""

import json
from datetime import timedelta

import pytest
from test_rc_causal import acceptance as acceptance
from test_rc_causal import causal as causal
from test_rc_tools import script


@pytest.fixture
def provenance(monkeypatch, causal, tmp_path):
    m = script(monkeypatch, "rc_host_witness")
    gate = script(monkeypatch, "rc_witness_provenance")
    c, challenge, trust, _, bundle, sign = causal
    from rc_acceptance import issue_challenge

    registry = tmp_path / "registry"
    issue_challenge(registry, challenge)
    original = bundle("A", review=False)
    sources = [
        r for r in original if r.evidence.payload.source not in {"host", "fault"}
    ]
    before = next(
        r.evidence.payload.snapshot
        for r in sources
        if getattr(r.evidence.payload, "phase", None) == "before"
    )
    base = before.observed_at
    hosts = [r for r in original if r.evidence.payload.source == "host"]
    roots, records, headers = [], [], []
    for i, host in enumerate(hosts):
        root = tmp_path / f"journal-{i}"
        root.mkdir(mode=0o700)
        op = host.evidence.payload.operations[0]
        header = dict(
            version=1,
            challenge_digest=m.request_digest(challenge.model_dump(mode="json")),
            environment_id=str(challenge.environment_id),
            staging_batch_digest=challenge.staging_batch_digest,
            case_id="A",
            host_identity=host.evidence.payload.host_digest,
            process_id=i + 100,
            process_nonce=str(i + 1) * 32,
            deployment_sha=challenge.deployment_sha,
            collector_digest="c" * 64,
            initial_snapshot_digest=m.request_digest(before.model_dump(mode="json")),
            started_at=base.isoformat(),
        )
        header = m.CollectorHeader.model_validate(header).model_dump(mode="json")
        observation = dict(
            operation=op.model_dump(
                mode="json",
                include={
                    "kind",
                    "target",
                    "outcome",
                    "at",
                    "lease_until",
                    "error_code",
                },
            ),
            target_step_ref=op.target_step_ref,
            run_ref=before.steps[0].run_ref,
            lease_digest=op.lease_digest,
            clock_error_seconds=0.0,
            source="store.claim",
            snapshot_digest="d" * 64,
            invocation=dict(
                invocation_id=str(i + 1) * 32,
                started_at=base.timestamp() + 4 + i,
                completed_at=base.timestamp() + 8 - i,
                clock_error_seconds=0.01,
                clock_source="postgresql",
                outcome="task" if i == 0 else "none",
            ),
        )
        if i == 1:
            observation["operation"]["at"] = base.timestamp() + 8
        record = dict(
            sequence=0,
            previous_digest=m.request_digest(header),
            observation=observation,
        )
        record["record_digest"] = m.request_digest(record)
        m._write(root / "header.json", header)
        m._write(root / "event-0000.json", record)
        m._write(
            root / "closed.json",
            dict(events=1, journal_digest=record["record_digest"], complete=True),
        )
        roots.append(root)
        records.append((header["host_identity"], record["record_digest"]))
        headers.append(m.request_digest(header))
    binding = m.ProvenanceBinding(
        challenge_digest=m.request_digest(challenge.model_dump(mode="json")),
        case_id="A",
        journals=records,
        fault_record_digest="f" * 64,
        process_headers=headers,
    )

    def bind(value):
        fault = c.CausalFault(
            case_id="A",
            phase="lease_competition",
            target_step_refs=[before.steps[0].ref],
            protected_record_digest=m.request_digest(value.model_dump(mode="json")),
        )
        return [*sources, sign(fault, "fault", start=1, end=20)]

    def check(**changes):
        return gate.precheck(
            **{
                "challenge": challenge,
                "registry": registry,
                "roots": roots,
                "binding": binding,
                "trust": trust,
                "receipts": bind(binding),
                "collector_digest": "c" * 64,
                **changes,
            }
        )

    def rewrite(index, change):
        root = roots[index]
        header = json.loads((root / "header.json").read_text("utf-8"))
        record = json.loads((root / "event-0000.json").read_text("utf-8"))
        change(header, record["observation"])
        record["previous_digest"] = m.request_digest(header)
        record.pop("record_digest")
        record["record_digest"] = m.request_digest(record)
        (root / "header.json").write_text(json.dumps(header), encoding="utf-8")
        (root / "event-0000.json").write_text(json.dumps(record), encoding="utf-8")
        (root / "closed.json").write_text(
            json.dumps(
                dict(events=1, journal_digest=record["record_digest"], complete=True)
            ),
            encoding="utf-8",
        )
        return m.ProvenanceBinding.model_validate(
            {
                **binding.model_dump(),
                "journals": [
                    (
                        json.loads((r / "header.json").read_text("utf-8"))[
                            "host_identity"
                        ],
                        json.loads((r / "closed.json").read_text("utf-8"))[
                            "journal_digest"
                        ],
                    )
                    for r in roots
                ],
                "process_headers": [
                    m.request_digest(json.loads((r / "header.json").read_text("utf-8")))
                    for r in roots
                ],
            }
        )

    return m, check, rewrite, binding, bind, roots, registry, challenge


def test_complete_protected_binding_is_only_an_unsigned_review_report(provenance):
    _, check, _, _, _, _, _, _ = provenance
    first = check()
    assert first == check()  # No local timestamp, paths or process IDs in report.
    assert first["eligible_for_signing"] is True
    assert first["case_a"] == "OVERLAPPING_INVOCATIONS"
    assert first["status"] == "NOT_VERIFIED" and first["unsigned"] is True
    assert first["transaction_contention"] == "NOT_VERIFIED"
    assert first["physical_hosts"] == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "attack",
    (
        "unsigned_chain",
        "copied_header",
        "wrong_pid",
        "wrong_nonce",
        "challenge",
        "batch",
        "case",
        "environment",
        "collector",
        "deployment",
        "digest",
        "duplicate_digest",
        "partial_binding",
        "missing_process_binding",
        "expired",
        "consumed",
        "clock_window",
        "database_only",
        "missing_host",
        "truncated",
        "reordered",
        "forged_boolean",
    ),
)
def test_untrusted_provenance_never_qualifies(provenance, attack):
    m, check, rewrite, binding, bind, roots, registry, challenge = provenance
    changes = {}
    if attack == "unsigned_chain":
        changes["receipts"] = []
    elif attack == "copied_header":
        header = json.loads((roots[0] / "header.json").read_text("utf-8"))
        rewrite(1, lambda h, o: h.update(header))
    elif attack in {
        "wrong_pid",
        "wrong_nonce",
        "challenge",
        "batch",
        "case",
        "environment",
        "collector",
        "deployment",
    }:
        field, value = {
            "wrong_pid": ("process_id", 999),
            "wrong_nonce": ("process_nonce", "e" * 32),
            "challenge": ("challenge_digest", "e" * 64),
            "batch": ("staging_batch_digest", "e" * 64),
            "case": ("case_id", "B"),
            "environment": ("environment_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
            "collector": ("collector_digest", "e" * 64),
            "deployment": ("deployment_sha", "e" * 40),
        }[attack]
        rewrite(1, lambda h, o: h.update({field: value}))
    elif attack in {
        "digest",
        "duplicate_digest",
        "partial_binding",
        "missing_process_binding",
    }:
        data = binding.model_dump()
        if attack == "digest":
            data["journals"] = [(binding.journals[0][0], "0" * 64), binding.journals[1]]
        elif attack == "duplicate_digest":
            data["journals"] = [
                binding.journals[0],
                (binding.journals[1][0], binding.journals[0][1]),
            ]
        elif attack == "partial_binding":
            changes["roots"] = roots[:1]
        else:
            data["process_headers"] = []
        changed = m.ProvenanceBinding.model_validate(data)
        changes.update(binding=changed, receipts=bind(changed))
    elif attack == "expired":
        expired = challenge.model_copy(
            update={"expires_at": challenge.issued_at + timedelta(seconds=1)}
        )
        (registry / "issued.json").write_text(
            expired.model_dump_json(), encoding="utf-8"
        )
        changes["challenge"] = expired
    elif attack == "consumed":
        from rc_acceptance import consume_challenge

        consume_challenge(registry, challenge)
    elif attack == "clock_window":
        changed = rewrite(
            1,
            lambda h, o: o["invocation"].update(
                started_at=challenge.issued_at.timestamp(), clock_error_seconds=2.0
            ),
        )
        changes.update(binding=changed, receipts=bind(changed))
    elif attack == "database_only":
        changed = rewrite(
            1,
            lambda h, o: o["operation"].update(
                outcome="unavailable", error_code="DatabaseUnavailable"
            ),
        )
        changes.update(binding=changed, receipts=bind(changed))
    elif attack == "missing_host":
        changes["roots"] = roots[:1]
    elif attack == "truncated":
        (roots[1] / "event-0000.json").write_text('{"sequence":', encoding="utf-8")
    elif attack == "reordered":
        rewrite(1, lambda h, o: None)
        value = json.loads((roots[1] / "event-0000.json").read_text("utf-8"))
        value["sequence"] = 1
        (roots[1] / "event-0000.json").write_text(json.dumps(value), encoding="utf-8")
    else:
        rewrite(1, lambda h, o: o.update(concurrent=True))
    report = check(**changes)
    assert report["status"] == "NOT_VERIFIED"
    assert report["eligible_for_signing"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "kind", ("sequential", "uncertain", "outside_fault", "replayed_invocation")
)
def test_signed_binding_cannot_turn_inadequate_timing_into_contention(provenance, kind):
    _, check, rewrite, _, bind, _, _, challenge = provenance
    base = challenge.issued_at.timestamp() + 1

    def change(header, observed):
        if kind == "sequential":
            observed["invocation"].update(started_at=base + 9, completed_at=base + 10)
            observed["operation"]["at"] = base + 11
        elif kind == "uncertain":
            observed["invocation"].update(
                started_at=base + 7.99, completed_at=base + 9, clock_error_seconds=0.1
            )
            observed["operation"]["at"] = base + 10
        elif kind == "replayed_invocation":
            observed["invocation"]["invocation_id"] = "1" * 32
        else:
            observed["invocation"].update(started_at=base + 21, completed_at=base + 22)

    binding = rewrite(1, change)
    result = check(binding=binding, receipts=bind(binding))
    assert (
        result["eligible_for_signing"] is False and result["status"] == "NOT_VERIFIED"
    )
    if kind == "sequential":
        assert result["case_a"] == "SEQUENTIAL_VALID_LEASE"
    elif kind == "uncertain":
        assert result["case_a"] == "TIMING_UNCERTAIN"


def test_historical_binding_serialization_and_digest_are_unchanged(monkeypatch):
    m = script(monkeypatch, "rc_host_witness")
    data = dict(
        version=1,
        challenge_digest="1" * 64,
        case_id="A",
        journals=[["2" * 64, "3" * 64], ["4" * 64, "5" * 64]],
        fault_record_digest="6" * 64,
    )
    parsed = m.ProvenanceBinding.model_validate(data)
    assert parsed.model_dump(mode="json") == data
    assert m.request_digest(parsed.model_dump(mode="json")) == m.request_digest(data)


def test_precheck_cli_rejects_unpinned_trust_without_connecting(
    monkeypatch, tmp_path, capsys
):
    gate = script(monkeypatch, "rc_witness_provenance")
    trust = tmp_path / "trust.json"
    trust.write_text('{"PRIVATE_TEST_CREDENTIAL":"never-output"}', encoding="utf-8")
    import rc_staging

    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **k: pytest.fail("OfflinePrecheckConnected")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "rc_witness_provenance.py",
            "--trust",
            str(trust),
            "--trust-sha256",
            "0" * 64,
            "--collector-sha256",
            "1" * 64,
            "--challenge",
            "unused",
            "--state-dir",
            "unused",
            "--binding",
            "unused",
            "--journal",
            "unused",
        ],
    )
    assert gate.main() == 2
    value = capsys.readouterr()
    assert "PRIVATE" not in value.out and "never-output" not in value.out
    assert json.loads(value.out)["eligible_for_signing"] is False
