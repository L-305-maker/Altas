"""Publication, interruption and signer pinning using synthetic A-J evidence."""

import base64
import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from custody_publication_worker import run_worker
from test_evidence_custody import custody, frozen, prepared
from test_rc_release import acceptance as acceptance
from test_rc_release import causal as causal
from test_rc_release import release_bundle as release_bundle
from test_rc_tools import script


def conflicting_sources(f):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from rc_acceptance import TrustedSigner

    original = (f.bundle / "build.json").read_bytes()
    receipt = f.m.CausalSignedEvidence.model_validate_json(original)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    identity = f.m.request_digest(public)
    signer = next(s for s in f.trust.signers if s.role == "build")
    second = TrustedSigner.model_validate(
        {**signer.model_dump(), "key_id": identity, "public_key": public}
    )
    f.keys[identity] = key
    f.trust = f.trust.model_copy(update={"signers": (*f.trust.signers, second)})
    f.write(f.args.trust, f.trust)
    f.args.trust_sha256 = hashlib.sha256(Path(f.args.trust).read_bytes()).hexdigest()
    replacement = f.sign(
        receipt.evidence.payload,
        "build",
        1,
        start=receipt.evidence.started_at,
        end=receipt.evidence.completed_at,
    )
    f.write(f.bundle / "build.json", replacement)
    for case in "ABCDEFGHIJ":
        f.review(case)
    assert {
        c.status
        for c in f.m.verify_causal_acceptance(f.challenge, f.trust, f.receipts()).cases
    } == {"PASS"}
    requests, returned = f.root / "requests", {}
    requests.mkdir()
    for path in [*f.bundle.glob("*.json"), *f.bundle.glob("*/*.json")]:
        if path.name == "binding.json":
            continue
        raw = path.read_bytes()
        returned[path] = raw
        target = requests / path.relative_to(f.bundle)
        target.parent.mkdir(parents=True, exist_ok=True)
        f.write(target, json.loads(raw)["evidence"])
        path.unlink()
    (f.bundle / "build.json").write_bytes(original)
    return requests, returned, original


def test_frozen_signed_identity_cannot_be_shadowed_by_another_trusted_request(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    requests, returned, original = conflicting_sources(f)
    store = module.init_store(f.root / "custody")
    try:
        first = module.freeze(f.args, store, stage="prepared", requests=requests)
        assert (
            module.load(store, module.digest(first)).data["bundle/build.json"]
            == original
        )
        for path, raw in returned.items():
            path.write_bytes(raw)
        final = module.freeze(
            f.args, store, stage="final", prepared_digest=module.digest(first)
        )
    except ValueError as error:
        assert error.args == ("CustodySourceRequestConflict",)
        return
    f.args.custody_store, f.args.manifest_sha256 = str(store), module.digest(final)
    f.args.command = "custody-precheck"
    code, _ = script(monkeypatch, "verify_rc_release").run(f.args)
    assert code != 0, "Frozen signed A was silently replaced by independently trusted B"


def test_even_identical_signed_source_and_request_are_rejected(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    requests = f.root / "requests"
    requests.mkdir()
    f.write(
        requests / "build.json",
        json.loads((f.bundle / "build.json").read_bytes())["evidence"],
    )
    with pytest.raises(ValueError, match="CustodySourceRequestConflict"):
        module.freeze(f.args, store, stage="prepared", requests=requests)


def test_legacy_conflicting_prepared_cannot_enter_a_new_chain(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    requests = f.root / "requests"
    requests.mkdir()
    f.write(
        requests / "build.json",
        json.loads((f.bundle / "build.json").read_bytes())["evidence"],
    )
    # Construct an old-format conflicting object without weakening the new read.
    with monkeypatch.context() as legacy:
        legacy.setattr(module, "reject_source_request_conflicts", lambda data: None)
        manifest = module.freeze(f.args, store, stage="prepared", requests=requests)
    with pytest.raises(ValueError, match="CustodySourceRequestConflict"):
        module.load(store, module.digest(manifest))
    with pytest.raises(ValueError, match="CustodySourceRequestConflict"):
        module.freeze(
            f.args, store, stage="final", prepared_digest=module.digest(manifest)
        )


def test_handoff_domain_is_the_actual_single_lf_prefix(monkeypatch, release_bundle):
    f = release_bundle
    module, store, manifest = prepared(f, monkeypatch)
    request = module.handoff(store, module.digest(manifest), "A/host-0.json")
    raw = base64.b64decode(request["signing_bytes_base64"])
    domain = request["signing_domain"].encode("utf-8")
    assert domain == b"Altas.RCCausalEvidence.v3\n"
    assert domain[-1] == 10 and domain.count(b"\n") == 1
    assert raw.startswith(domain)
    assert raw[len(domain) :] == f.m.canonical_json(request["evidence"]).encode("utf-8")


def child_inputs(f, module):
    store = module.init_store(f.root / "custody")
    module.bind_registry(store, f.challenge, f.registry, create=True)
    path = f.root / "child-input.json"
    f.write(path, {"args": vars(f.args), "store": str(store)})
    return store, path


def start_child(input_file, phase, trace, barrier=None):
    child = multiprocessing.get_context("spawn").Process(
        target=run_worker, args=(str(input_file), phase, str(trace), barrier)
    )
    child.start()
    return child


def finish_child(child):
    child.join(timeout=45)
    if child.is_alive():
        child.terminate()  # Only the child created by this test.
        child.join(timeout=10)
        pytest.fail("Owned publication child did not finish")
    return child.exitcode


@pytest.mark.parametrize(
    "phase", ("allocated", "partial", "before_manifest", "manifest_written")
)
def test_abrupt_exit_never_publishes_and_retry_needs_no_deletion(
    monkeypatch, release_bundle, phase
):
    f = release_bundle
    module = custody(monkeypatch)
    store, inputs = child_inputs(f, module)
    trace_file = f.root / "trace.json"
    original = {str(p): p.read_bytes() for p in f.bundle.rglob("*.json")}
    child = start_child(inputs, phase, trace_file)
    assert finish_child(child) == 73
    trace = json.loads(trace_file.read_bytes())
    with pytest.raises((ValueError, FileNotFoundError)):
        module.load(store, trace["manifest_digest"])
    f.args.custody_store, f.args.manifest_sha256 = str(store), trace["manifest_digest"]
    f.args.command = "custody-precheck"
    assert script(monkeypatch, "verify_rc_release").run(f.args)[0] == 2
    assert (store / trace["work_name"]).is_dir()
    result = module.freeze(f.args, store, stage="final")
    assert module.digest(result) == trace["manifest_digest"]
    assert module.load(store, module.digest(result)).manifest == result
    assert (store / trace["work_name"]).is_dir()  # Audit evidence was not removed.
    assert original == {str(p): p.read_bytes() for p in f.bundle.rglob("*.json")}
    assert not (f.registry / "consumed.json").exists()


def test_completed_object_is_revalidated_idempotently_without_overwrite(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    store, inputs = child_inputs(f, module)
    trace = f.root / "complete.json"
    assert finish_child(start_child(inputs, "complete", trace)) == 0
    first = json.loads(trace.read_bytes())
    before = module.load(store, first["manifest_digest"])
    assert finish_child(start_child(inputs, "complete", f.root / "again.json")) == 0
    second = module.load(store, first["manifest_digest"])
    assert before.data == second.data and before.identities == second.identities
    assert (
        json.loads((f.root / "again.json").read_bytes())["manifest_digest"]
        == first["manifest_digest"]
    )


def test_two_processes_publish_one_complete_identical_object(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    store, inputs = child_inputs(f, module)
    barrier = multiprocessing.get_context("spawn").Barrier(2)
    children = [
        start_child(inputs, "concurrent", f.root / f"trace-{i}.json", barrier)
        for i in range(2)
    ]
    assert [finish_child(child) for child in children] == [0, 0]
    records = [json.loads((f.root / f"trace-{i}.json").read_bytes()) for i in range(2)]
    assert len({r["manifest_digest"] for r in records}) == 1
    assert module.load(store, records[0]["manifest_digest"]).manifest.stage == "final"


@pytest.mark.parametrize("occupied", (False, True))
def test_native_publication_never_replaces_an_existing_directory(
    monkeypatch, release_bundle, occupied
):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    source, destination = store / (".pending-" + "a" * 32), store / ("b" * 64)
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    (source / "evidence.bin").write_bytes(b"new")
    if occupied:
        (destination / "evidence.bin").write_bytes(b"retained")
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    with pytest.raises(OSError):
        module.publish_no_replace(source, destination)
    assert (source / "evidence.bin").read_bytes() == b"new"
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before


@pytest.mark.parametrize("attack", ("bytes", "manifest", "extra", "hardlink"))
def test_idempotency_rejects_and_preserves_corrupt_existing_objects(
    monkeypatch, release_bundle, attack
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    root = store / module.digest(manifest)
    target = root / "files/bundle/build.json"
    if attack == "bytes":
        target.write_bytes(target.read_bytes() + b" ")
    elif attack == "manifest":
        (root / "manifest.json").write_bytes(b"{}")
    elif attack == "extra":
        (root / "files/extra.json").write_bytes(b"{}")
    else:
        os.link(target, f.root / "alias.json")
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    with pytest.raises(ValueError, match="CustodyPublicationConflict"):
        module.freeze(f.args, store, stage="final")
    assert before == {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert not (f.registry / "consumed.json").exists()


def test_forced_same_digest_different_bytes_does_not_overwrite(
    monkeypatch, release_bundle
):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    identifier, actual_digest = module.digest(manifest), module.digest
    original = module.load(store, identifier)
    path = f.bundle / "build.json"
    path.write_bytes(
        path.read_bytes() + b" "
    )  # Same valid signed envelope, different raw bytes.
    monkeypatch.setattr(
        module,
        "digest",
        lambda value: (
            identifier
            if isinstance(value, module.EvidenceCustodyManifest)
            else actual_digest(value)
        ),
    )
    with pytest.raises(ValueError, match="CustodyPublicationConflict"):
        module.freeze(f.args, store, stage="final")
    assert module.load(store, identifier).data == original.data


def test_partial_copy_mismatch_is_never_published(monkeypatch, release_bundle):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    actual = module.write_new

    def write(path, raw):
        actual(path, raw)
        if Path(path).name == "build.json":
            Path(path).write_bytes(raw + b" ")

    monkeypatch.setattr(module, "write_new", write)
    with pytest.raises(ValueError, match="CustodySnapshotChanged"):
        module.freeze(f.args, store, stage="final")
    assert list(store.glob(".pending-*"))
    assert not any(len(p.name) == 64 for p in store.iterdir())


def test_legacy_partial_needs_explicit_audited_quarantine(monkeypatch, release_bundle):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    identifier = module.digest(manifest)
    root = store / identifier
    with pytest.raises(ValueError, match="CustodyCompletedObjectProtected"):
        module.quarantine(store, identifier)
    root.rename(f.root / "retained-complete")
    root.mkdir(mode=0o700)
    (root / "partial.json").write_bytes(b'{"audit":"retain"}')
    anchor = {p.name: p.read_bytes() for p in store.glob("*.json")}
    with pytest.raises(ValueError, match="CustodyPublicationConflict"):
        module.freeze(f.args, store, stage="final")
    record = module.quarantine(store, identifier)
    assert (
        store / record["target_name"] / "partial.json"
    ).read_bytes() == b'{"audit":"retain"}'
    assert (
        json.loads(
            (store / f"quarantine-intent-{record['operation']}.json").read_bytes()
        )
        == record
    )
    assert (
        json.loads((store / f"quarantine-done-{record['operation']}.json").read_bytes())
        == record
    )
    assert module.freeze(f.args, store, stage="final") == manifest
    assert anchor == {name: (store / name).read_bytes() for name in anchor}
    with pytest.raises(ValueError):
        module.quarantine(store, "../retained-complete")
    assert not (f.registry / "consumed.json").exists()


@pytest.mark.parametrize("reason", ("spent", "consumed", "trust", "challenge"))
def test_retry_cannot_bypass_registration_or_pins(monkeypatch, release_bundle, reason):
    f = release_bundle
    module, store, manifest = frozen(f, monkeypatch)
    original = module.load(store, module.digest(manifest))
    if reason == "spent":
        (store / f"spent-{module.digest(f.challenge)}.json").write_bytes(b"{}")
    elif reason == "consumed":
        (f.registry / "consumed.json").write_bytes(b"{}")
    else:
        path = Path(getattr(f.args, reason))
        path.write_bytes(path.read_bytes() + b" " if reason == "trust" else b"{}")
    with pytest.raises(ValueError):
        module.freeze(f.args, store, stage="final")
    assert module.load(store, module.digest(manifest)).data == original.data


def test_already_signed_sources_are_pinned_and_not_handed_off(
    monkeypatch, release_bundle
):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    first = module.freeze(f.args, store, stage="prepared")
    with pytest.raises(ValueError, match="CustodyAlreadySignedSource"):
        module.handoff(store, module.digest(first), "build.json")
    final = module.freeze(
        f.args, store, stage="final", prepared_digest=module.digest(first)
    )
    assert (
        module.load(store, module.digest(first)).data
        == module.load(store, module.digest(final)).data
    )
    path = f.bundle / "build.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="CustodySourceChanged"):
        module.freeze(
            f.args, store, stage="final", prepared_digest=module.digest(first)
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX primitive check; Windows uses native no-replace rename",
)
def test_missing_atomic_primitive_fails_closed(monkeypatch, release_bundle):
    f = release_bundle
    module = custody(monkeypatch)
    store = module.init_store(f.root / "custody")
    work = store / (".pending-" + "a" * 32)
    work.mkdir(mode=0o700)
    monkeypatch.setattr(module.sys, "platform", "unsupported")
    with pytest.raises(ValueError, match="CustodyAtomicPublicationUnavailable"):
        module.publish_no_replace(work, store / ("b" * 64))
    assert work.is_dir() and not (store / ("b" * 64)).exists()
