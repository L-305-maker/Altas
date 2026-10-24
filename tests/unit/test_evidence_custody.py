"""Owned-file races and synthetic signatures; no physical custody attestation."""

import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from test_rc_release import acceptance as acceptance
from test_rc_release import causal as causal
from test_rc_release import release_bundle as release_bundle
from test_rc_tools import script


def custody(monkeypatch):
    return script(monkeypatch, "rc_evidence_custody")


def frozen(f, monkeypatch):
    module = custody(monkeypatch)
    store = f.root / "custody"
    module.init_store(store)
    manifest = module.freeze(f.args, store, stage="final")
    f.args.custody_store = str(store)
    f.args.manifest_sha256 = module.digest(manifest)
    return module, store, manifest


def test_characterize_legacy_path_change_after_evaluate(monkeypatch, release_bundle):
    """Reproduce the conditional risk without changing historical v1 behavior."""
    f = release_bundle
    gate = script(monkeypatch, "verify_rc_release")
    actual = gate.evaluate

    def evaluate(args):
        report = actual(args)
        (f.bundle / "build.json").write_text("{}", encoding="utf-8")
        return report

    monkeypatch.setattr(gate, "evaluate", evaluate)
    f.args.command = "verify"
    code, report = gate.run(f.args)
    assert code == 0 and report.saved_evidence_validation == "PASS"
    assert (f.registry / "consumed.json").exists()
    assert (f.bundle / "build.json").read_text("utf-8") == "{}"


def test_custody_uses_frozen_bytes_after_evaluate(monkeypatch, release_bundle):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    actual = gate.evaluate

    def evaluate(args):
        assert Path(args.bundle) != f.bundle
        report = actual(args)
        (f.bundle / "build.json").write_text("{}", encoding="utf-8")
        return report

    monkeypatch.setattr(gate, "evaluate", evaluate)
    f.args.command = "custody-verify"
    code, report = gate.run(f.args)
    assert code == 0 and report.saved_evidence_validation == "PASS"
    assert report.custody_validation == "PASS" and report.schema_version == 2
    assert report.manifest_digest == module.digest(manifest)
    snapshot = module.load(store, report.manifest_digest)
    assert json.loads(snapshot.data["bundle/build.json"])["signature"]
    assert (f.registry / "consumed.json").exists()


def test_freeze_rejects_a_file_changed_during_read(monkeypatch, release_bundle):
    f = release_bundle
    module = custody(monkeypatch)
    store = f.root / "custody"
    module.init_store(store)
    actual = module.read_file
    changed = False

    def read(path, maximum):
        nonlocal changed
        data = actual(path, maximum)
        if Path(path) == f.bundle / "build.json" and not changed:
            changed = True
            Path(path).write_bytes(data + b" ")
        return data

    monkeypatch.setattr(module, "read_file", read)
    with pytest.raises(ValueError):
        module.freeze(f.args, store, stage="final")
    assert not (f.registry / "consumed.json").exists()


SOURCE_NAMES = (
    "bundle/build.json",
    "bundle/infrastructure.json",
    "bundle/A/before.json",
    "bundle/A/after.json",
    "bundle/A/fault.json",
    "bundle/A/host-0.json",
    "bundle/A/host-1.json",
    "bundle/A/reviewer.json",
    "bundle/A/binding.json",
    "bundle/A/host-0/header.json",
    "bundle/A/host-0/event-0000.json",
    "bundle/A/host-0/closed.json",
    "config.json",
    "v1.json",
    "plan.json",
    "challenge.json",
    "trust.json",
)


@pytest.mark.parametrize("name", SOURCE_NAMES)
def test_each_input_is_bound_to_actual_read_bytes(monkeypatch, release_bundle, name):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    actual, changed = module.read_file, False
    target = f.root / name

    def read(path, maximum):
        nonlocal changed
        raw = actual(path, maximum)
        if Path(path) == target and not changed:
            changed = True
            replacement = target.with_suffix(".replacement")
            replacement.write_bytes(raw + b" ")
            os.replace(replacement, target)
        return raw

    monkeypatch.setattr(module, "read_file", read)
    with pytest.raises(ValueError):
        module.freeze(f.args, store, stage="final")
    assert changed
    assert not (f.registry / "consumed.json").exists()


@pytest.mark.parametrize(
    "phase", ("receipt", "journal", "provenance", "evaluate", "consume")
)
def test_original_paths_cannot_switch_the_verified_object(
    monkeypatch, release_bundle, phase
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    hook = {
        "receipt": "read_json",
        "journal": "read_journal",
        "provenance": "provenance_precheck",
        "evaluate": "evaluate",
        "consume": "consume_challenge",
    }[phase]
    actual, changed = getattr(gate, hook), False

    def wrapped(*args, **kwargs):
        nonlocal changed

        def mutate():
            nonlocal changed
            changed = True
            for name in SOURCE_NAMES:
                (f.root / name).write_text("{}", encoding="utf-8")

        if phase == "consume" and not changed:
            mutate()
        value = actual(*args, **kwargs)
        if not changed and (phase != "receipt" or Path(args[0]).name == "build.json"):
            mutate()
        return value

    monkeypatch.setattr(gate, hook, wrapped)
    if phase == "journal":
        import rc_witness_provenance

        monkeypatch.setattr(rc_witness_provenance, "read_journal", wrapped)
    f.args.command = "custody-verify"
    code, report = gate.run(f.args)
    assert changed and code == 0 and report.saved_evidence_validation == "PASS"
    assert report.manifest_digest == module.digest(manifest)
    assert module.load(store, report.manifest_digest).data["bundle/build.json"] != b"{}"


@pytest.mark.parametrize("phase", ("evaluate", "consume"))
def test_protected_copy_tampering_cannot_return_pass(
    monkeypatch, release_bundle, phase
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    hook = "evaluate" if phase == "evaluate" else "consume_challenge"
    actual = getattr(gate, hook)

    def wrapped(*args, **kwargs):
        value = actual(*args, **kwargs)
        (
            module.load(store, module.digest(manifest)).root / "bundle/build.json"
        ).write_text("{}", encoding="utf-8")
        return value

    monkeypatch.setattr(gate, hook, wrapped)
    f.args.command = "custody-verify"
    code, report = gate.run(f.args)
    assert code == 2 and report.custody_validation == "FAIL"


@pytest.mark.parametrize(
    "attack",
    (
        "hardlink",
        "missing",
        "extra",
        "duplicate",
        "nan",
        "oversize",
        "journal_order",
        "batch",
    ),
)
def test_filesystem_and_json_attacks_fail_closed(monkeypatch, release_bundle, attack):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    target = f.bundle / "build.json"
    if attack == "hardlink":
        os.link(target, f.root / "writer.json")
    elif attack == "missing":
        target.unlink()
    elif attack == "extra":
        (f.bundle / "extra.json").write_text("{}", encoding="utf-8")
    elif attack == "duplicate":
        target.write_text('{"evidence":{},"evidence":{}}', encoding="utf-8")
    elif attack == "nan":
        target.write_text('{"evidence":NaN}', encoding="utf-8")
    elif attack == "oversize":
        target.write_bytes(b" " * 2_100_001)
    elif attack == "journal_order":
        target = f.bundle / "A/host-0/event-0000.json"
        value = json.loads(target.read_bytes())
        value["sequence"] = 1
        target.write_text(json.dumps(value), encoding="utf-8")
    else:
        target = f.bundle / "A/host-0/header.json"
        value = json.loads(target.read_bytes())
        value["staging_batch_digest"] = "0" * 64
        target.write_text(json.dumps(value), encoding="utf-8")
    try:
        manifest = module.freeze(f.args, store, stage="final")
    except (ValueError, FileNotFoundError):
        return
    f.args.custody_store, f.args.manifest_sha256 = str(store), module.digest(manifest)
    f.args.command = "custody-precheck"
    assert script(monkeypatch, "verify_rc_release").run(f.args)[0] != 0


@pytest.mark.parametrize("kind", ("file_symlink", "directory_symlink", "junction"))
def test_real_filesystem_aliases_are_rejected(monkeypatch, release_bundle, kind):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    directory = kind != "file_symlink"
    original = f.bundle / "A/host-0" if directory else f.bundle / "build.json"
    moved = f.root / "alias-target"
    original.rename(moved)
    if kind == "junction":
        if os.name != "nt":
            pytest.skip(
                "Windows junction behavior requires Windows; symlinks run separately"
            )
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(original), str(moved)],
            capture_output=True,
        )
        assert result.returncode == 0
    else:
        try:
            original.symlink_to(moved, target_is_directory=directory)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                pytest.skip(
                    "Windows symlink privilege unavailable; Linux CI covers actual symlinks"
                )
            raise
    try:
        with pytest.raises(ValueError):
            module.freeze(f.args, store, stage="final")
    finally:
        if kind == "junction":
            original.rmdir()
        else:
            original.unlink()


@pytest.mark.parametrize("mutation", ("truncate", "append"))
def test_mutation_inside_open_read_interval_is_rejected(
    monkeypatch, release_bundle, mutation
):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    target = f.bundle / "build.json"
    actual, changed = module.os.open, False

    def opened(path, flags, *args, **kwargs):
        nonlocal changed
        fd = actual(path, flags, *args, **kwargs)
        if Path(path) == target and not changed:
            changed = True
            with target.open("ab" if mutation == "append" else "wb") as stream:
                stream.write(b" ")
        return fd

    monkeypatch.setattr(module.os, "open", opened)
    with pytest.raises(ValueError):
        module.freeze(f.args, store, stage="final")
    assert changed


def test_precheck_and_one_final_consumption_keep_historical_semantics(
    monkeypatch, release_bundle
):
    f = release_bundle
    frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    f.args.command = "custody-precheck"
    assert gate.run(f.args)[0] == 0
    assert not (f.registry / "consumed.json").exists()
    f.args.command = "custody-verify"
    assert gate.run(f.args)[0] == 0
    assert gate.run(f.args)[0] == 2


@pytest.mark.parametrize("attack", ("copy", "clear", "recreate"))
def test_registry_cannot_be_reset_within_the_protected_store(
    monkeypatch, release_bundle, attack
):
    f = release_bundle
    frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    if attack == "copy":
        copied = f.root / "copied-registry"
        shutil.copytree(f.registry, copied)
        if os.name != "nt":
            copied.chmod(0o700)
        f.args.state_dir = str(copied)
    else:
        f.args.command = "custody-verify"
        assert gate.run(f.args)[0] == 0
        if attack == "clear":
            (f.registry / "consumed.json").unlink()
        else:
            f.registry.rename(f.root / "retired-registry")
            f.a.issue_challenge(f.registry, f.challenge)
    f.args.command = "custody-verify"
    assert gate.run(f.args)[0] == 2


def test_concurrent_final_custody_verification_has_one_winner(
    monkeypatch, release_bundle
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f = release_bundle
    module, _, _ = frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    boundary, actual = Barrier(2), gate.evaluate

    def evaluate(args):
        result = actual(args)
        boundary.wait(timeout=30)
        return result

    monkeypatch.setattr(gate, "evaluate", evaluate)
    f.args.command = "custody-verify"
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(gate.run, f.args) for _ in range(2)]
        assert sorted(f.result(timeout=60)[0] for f in futures) == [0, 2]


def prepared(f, monkeypatch, *, reviewer=False):
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    requests = f.root / "requests"
    requests.mkdir()
    saved = {}
    for path in [*f.bundle.glob("*.json"), *f.bundle.glob("*/*.json")]:
        if path.name == "binding.json":
            continue
        raw = path.read_bytes()
        value = json.loads(raw)
        saved[path] = raw
        if reviewer or path.name != "reviewer.json":
            target = requests / path.relative_to(f.bundle)
            target.parent.mkdir(parents=True, exist_ok=True)
            f.write(target, value["evidence"])
        path.unlink()
    manifest = module.freeze(f.args, store, stage="prepared", requests=requests)
    for path, raw in saved.items():
        path.write_bytes(raw)
    return module, store, manifest


def test_unsigned_handoff_and_exact_external_returns(monkeypatch, release_bundle):
    import base64

    from rc_causal import signing_bytes

    f = release_bundle
    module, store, manifest = prepared(f, monkeypatch)
    request = module.handoff(store, module.digest(manifest), "A/host-0.json")
    assert request["status"] == "UNSIGNED"
    value = f.m.CausalSignedEvidence.model_validate_json(
        (f.bundle / "A/host-0.json").read_bytes()
    )
    assert base64.b64decode(request["signing_bytes_base64"]) == signing_bytes(
        value.evidence
    )
    assert request["evidence_digest"] == value.evidence_digest
    review_requests = f.root / "review-requests"
    saved_reviews = {}
    for path in f.bundle.glob("*/reviewer.json"):
        raw = path.read_bytes()
        saved_reviews[path] = raw
        target = review_requests / path.relative_to(f.bundle)
        target.parent.mkdir(parents=True, exist_ok=True)
        f.write(target, json.loads(raw)["evidence"])
        path.unlink()
    review_manifest = module.freeze(
        f.args,
        store,
        stage="prepared",
        prepared_digest=module.digest(manifest),
        requests=review_requests,
    )
    review_request = module.handoff(
        store, module.digest(review_manifest), "A/reviewer.json"
    )
    assert (
        review_request["role"] == "reviewer" and review_request["status"] == "UNSIGNED"
    )
    for path, raw in saved_reviews.items():
        path.write_bytes(raw)
    final = module.freeze(
        f.args, store, stage="final", prepared_digest=module.digest(review_manifest)
    )
    f.args.custody_store, f.args.manifest_sha256 = str(store), module.digest(final)
    f.args.command = "custody-verify"
    code, report = script(monkeypatch, "verify_rc_release").run(f.args)
    assert code == 0 and report.prepared_manifest_digest == module.digest(
        review_manifest
    )
    assert report.production_release == report.physical_hosts == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "attack", ("envelope", "role", "payload", "source", "reviewer")
)
def test_signer_returns_cannot_drift(monkeypatch, release_bundle, attack):
    from rc_causal import CausalEvidence, signing_bytes

    f = release_bundle
    module, store, manifest = prepared(f, monkeypatch, reviewer=True)
    if attack == "source":
        path = f.bundle / "A/host-0/header.json"
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path = f.bundle / ("A/reviewer.json" if attack == "reviewer" else "build.json")
        value = json.loads(path.read_bytes())
        if attack == "envelope":
            value["evidence"]["started_at"] = (
                f.base + timedelta(microseconds=1)
            ).isoformat()
            value["evidence"]["completed_at"] = value["evidence"]["started_at"]
        elif attack == "role":
            value["evidence"]["attestation_identity"] = f.trust.signers[0].key_id
        elif attack == "payload":
            value["evidence"]["payload"]["identity"]["runtime_digest"] = "d" * 64
        else:
            value["evidence"]["payload"]["reviewed_evidence_digest"] = "d" * 64
        evidence = CausalEvidence.model_validate(value["evidence"])
        value["evidence_digest"] = module.digest(evidence)
        value["signature"] = (
            f.keys[evidence.attestation_identity].sign(signing_bytes(evidence)).hex()
        )
        f.write(path, value)
    with pytest.raises(ValueError):
        module.freeze(
            f.args, store, stage="final", prepared_digest=module.digest(manifest)
        )


def test_manifest_identity_bounds_and_private_store_cannot_be_faked(
    monkeypatch, release_bundle
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    with pytest.raises(FileExistsError):
        module.freeze(f.args, store, stage="final")
    root = store / module.digest(manifest)
    renamed = store / ("d" * 64)
    root.rename(renamed)
    with pytest.raises(ValueError):
        module.load(store, "d" * 64)
    with pytest.raises(FileNotFoundError):
        module.load(store, module.digest(manifest))


def test_total_byte_limit_is_enforced(monkeypatch, release_bundle):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    monkeypatch.setattr(module, "MAX_TOTAL", 500)
    with pytest.raises(ValueError):
        module.freeze(f.args, store, stage="final")


@pytest.mark.parametrize(
    "outcome", ("missing_review", "host_mismatch", "bad_signature")
)
def test_final_fail_and_incomplete_consumption_contract(
    monkeypatch, release_bundle, outcome
):
    f = release_bundle
    if outcome == "missing_review":
        (f.bundle / "A/reviewer.json").unlink()
    elif outcome == "host_mismatch":
        f.change_host("A", 0, lambda value: value.update(started_at=f.base.isoformat()))
    else:
        path = f.bundle / "A/host-0.json"
        value = json.loads(path.read_bytes())
        value["signature"] = "0" * 128
        f.write(path, value)
    _, store, _ = frozen(f, monkeypatch)
    f.args.command = "custody-verify"
    code, report = script(monkeypatch, "verify_rc_release").run(f.args)
    assert code == (1 if outcome == "missing_review" else 2)
    assert report.saved_evidence_validation == (
        "NOT_VERIFIED" if outcome == "missing_review" else "FAIL"
    )
    assert (f.registry / "consumed.json").exists() == (outcome != "bad_signature")
    assert bool(list(store.glob("spent-*.json"))) == (outcome != "bad_signature")


@pytest.mark.parametrize("phase", ("before", "consume"))
def test_expiry_is_checked_again_at_final_consumption(
    monkeypatch, release_bundle, phase
):
    from datetime import datetime

    import rc_acceptance
    import rc_host_witness

    f = release_bundle
    frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return f.challenge.expires_at + timedelta(seconds=1)

    def expire():
        monkeypatch.setattr(rc_host_witness, "datetime", Expired)
        monkeypatch.setattr(rc_acceptance, "datetime", Expired)

    if phase == "before":
        expire()
    else:
        actual = gate.consume_challenge

        def consume(*args):
            expire()
            actual(*args)

        monkeypatch.setattr(gate, "consume_challenge", consume)
    f.args.command = "custody-verify"
    assert gate.run(f.args)[0] == 2


def test_registry_identity_cannot_change_inside_consume(monkeypatch, release_bundle):
    f = release_bundle
    frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    actual = gate.consume_challenge

    def consume(*args):
        f.registry.rename(f.root / "original-registry")
        f.a.issue_challenge(f.registry, f.challenge)
        actual(*args)

    monkeypatch.setattr(gate, "consume_challenge", consume)
    f.args.command = "custody-verify"
    assert gate.run(f.args)[0] == 2


def test_registry_swap_and_restore_cannot_hide_a_missing_original_tombstone(
    monkeypatch, release_bundle
):
    f = release_bundle
    frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    actual = gate.consume_challenge

    def consume(*args):
        original = f.root / "original-registry"
        f.registry.rename(original)
        f.a.issue_challenge(f.registry, f.challenge)
        actual(*args)
        f.registry.rename(f.root / "substitute-registry")
        original.rename(f.registry)

    monkeypatch.setattr(gate, "consume_challenge", consume)
    f.args.command = "custody-verify"
    code, report = gate.run(f.args)
    assert code == 2 and report.custody_validation == "FAIL"
    assert not (f.registry / "consumed.json").exists()


def materials(f, module, manifest):
    records = {r.path: r.sha256 for r in manifest.files}
    value = {
        "schema_version": 1,
        "environment_id": str(f.challenge.environment_id),
        "scope_approval_digest": "1" * 64,
        "operator_identity": "2" * 64,
        "independent_approver_identity": "3" * 64,
        "host_inventory_digest": "4" * 64,
        "worker_hosts": ["5" * 64, "6" * 64],
        "deployment_sha": f.challenge.deployment_sha,
        "trust_sha256": f.args.trust_sha256,
        "collector_sha256": f.args.collector_sha256,
        "private_plan_sha256": records["plan.json"],
        "preflight_sha256": records["v1.json"],
        "database_oid": f.config.database_oid,
        "database_marker": str(f.challenge.environment_id),
        "tls_preflight_digest": "7" * 64,
        "alembic_revision": "0008",
        "synthetic_task_limit": 20,
        "fault_window_start": f.base.isoformat(),
        "fault_window_end": (f.base + timedelta(seconds=30)).isoformat(),
        "stop_conditions_digest": "8" * 64,
        "unknown_recovery_runbook_digest": "9" * 64,
        "lease_recovery_runbook_digest": "a" * 64,
        "private_retention_policy_digest": "b" * 64,
        "aj_runbook_digest": "c" * 64,
    }
    path = f.root / "operator-materials.json"
    f.write(path, value)
    return path, value


@pytest.mark.parametrize(
    "missing",
    (
        None,
        "scope_approval_digest",
        "host_inventory_digest",
        "trust_sha256",
        "collector_sha256",
        "private_plan_sha256",
        "tls_preflight_digest",
        "independent_approver_identity",
        "stop_conditions_digest",
        "unknown_recovery_runbook_digest",
        "lease_recovery_runbook_digest",
        "private_retention_policy_digest",
        "aj_runbook_digest",
    ),
)
def test_operator_readiness_is_only_a_material_check(
    monkeypatch, release_bundle, missing
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    path, value = materials(f, module, manifest)
    if missing:
        value.pop(missing)
        f.write(path, value)
    readiness = script(monkeypatch, "rc_operator_readiness")
    result = readiness.readiness(
        store,
        module.digest(manifest),
        f.registry,
        path,
        f.args.trust_sha256,
        f.args.collector_sha256,
    )
    assert result["status"] == ("NOT_READY" if missing else "GO_FOR_OPERATOR_REVIEW")
    assert result["execution_authorized"] is False
    assert result["production_release"] == "NOT_VERIFIED"
    assert not (f.registry / "consumed.json").exists()


@pytest.mark.parametrize(
    "field",
    (
        "independent_approver_identity",
        "worker_hosts",
        "deployment_sha",
        "private_plan_sha256",
        "database_oid",
        "synthetic_task_limit",
        "fault_window_end",
    ),
)
def test_operator_materials_must_match_frozen_scope(monkeypatch, release_bundle, field):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    path, value = materials(f, module, manifest)
    value[field] = {
        "independent_approver_identity": value["operator_identity"],
        "worker_hosts": ["5" * 64, "5" * 64],
        "deployment_sha": "d" * 40,
        "private_plan_sha256": "d" * 64,
        "database_oid": 1,
        "synthetic_task_limit": 1,
        "fault_window_end": (f.base + timedelta(hours=3)).isoformat(),
    }[field]
    f.write(path, value)
    result = script(monkeypatch, "rc_operator_readiness").readiness(
        store,
        module.digest(manifest),
        f.registry,
        path,
        f.args.trust_sha256,
        f.args.collector_sha256,
    )
    assert result["status"] == "NOT_READY"


def test_versioned_reports_are_readable_without_reinterpreting_v1(
    monkeypatch, release_bundle
):
    f = release_bundle
    gate = script(monkeypatch, "verify_rc_release")
    old = gate.evaluate(f.args)
    assert gate.ReleaseReport.model_validate_json(old.model_dump_json()) == old
    frozen(f, monkeypatch)
    f.args.command = "custody-precheck"
    code, new = gate.run(f.args)
    assert code == 0
    assert gate.CustodyReleaseReport.model_validate_json(new.model_dump_json()) == new
    assert new.model_dump() == gate.run(f.args)[1].model_dump()
    assert str(f.root) not in new.model_dump_json()


def test_custody_cli_and_readiness_are_offline(monkeypatch, release_bundle, capsys):
    import rc_staging

    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **k: pytest.fail("OfflineCustodyConnected")
    )
    gate = script(monkeypatch, "verify_rc_release")
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_rc_release.py",
            "custody-precheck",
            "--custody-store",
            str(store),
            "--manifest-sha256",
            module.digest(manifest),
            "--state-dir",
            str(f.registry),
            "--trust-sha256",
            f.args.trust_sha256,
            "--collector-sha256",
            f.args.collector_sha256,
        ],
    )
    assert gate.main() == 0
    text = capsys.readouterr().out
    assert json.loads(text)["custody_validation"] == "PASS" and str(f.root) not in text
    path, _ = materials(f, module, manifest)
    readiness = script(monkeypatch, "rc_operator_readiness")
    monkeypatch.setattr(
        "sys.argv",
        [
            "rc_operator_readiness.py",
            "--store",
            str(store),
            "--manifest-sha256",
            module.digest(manifest),
            "--state-dir",
            str(f.registry),
            "--materials",
            str(path),
            "--trust-sha256",
            f.args.trust_sha256,
            "--collector-sha256",
            f.args.collector_sha256,
        ],
    )
    assert readiness.main() == 0
    assert json.loads(capsys.readouterr().out)["execution_authorized"] is False


def test_even_transient_copy_replacement_cannot_change_the_read_version(
    monkeypatch, release_bundle
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    gate = script(monkeypatch, "verify_rc_release")
    actual, observed = gate.read_json, []
    target = module.load(store, module.digest(manifest)).root / "bundle/build.json"
    original = target.read_bytes()

    def read(path, maximum):
        if Path(path) != target:
            return actual(path, maximum)
        target.write_text("{}", encoding="utf-8")
        try:
            value = actual(path, maximum)
            observed.append(value == json.loads(original))
            return value
        finally:
            target.write_bytes(original)

    monkeypatch.setattr(gate, "read_json", read)
    f.args.command = "custody-verify"
    code, report = gate.run(f.args)
    assert observed == [True]  # Only the captured bytes enter any verifier.
    assert (
        code == 2 and report.custody_validation == "FAIL"
    )  # Metadata detects the transient write.
    assert not (f.registry / "consumed.json").exists()


def test_frozen_read_contexts_are_isolated_and_reset(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from verify_rc_acceptance import bounded_json, frozen_input_bytes

    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root / "input.json").write_text("999", encoding="utf-8")
    boundary = Barrier(2)

    def run(i):
        with frozen_input_bytes(roots[i], {"input.json": str(i).encode()}):
            boundary.wait(timeout=10)
            value = bounded_json(roots[i] / "input.json", 100)
        assert bounded_json(roots[i] / "input.json", 100) == 999
        return value

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, range(2))) == [0, 1]
