"""Private, content-addressed RC copies; hashes do not authenticate an owner/admin.

The operator must retain ONE protected store and its original registry. Copying
or resetting the whole trusted domain is outside this single-machine protocol.
No keys, signing, DB connections, fault execution or production authorization.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Literal
from uuid import UUID

from pydantic import Field
from rc_acceptance import AcceptanceTrust, Digest, Frozen, evidence_set_digest
from rc_causal import (
    CausalChallenge,
    CausalEvidence,
    CausalSignedEvidence,
    signing_bytes,
    verify_causal_acceptance,
)
from rc_host_witness import MAX_BYTES, check_registration
from verify_rc_acceptance import frozen_input_bytes, reject_constant, unique_fields

from agentflow.application.ports.model_request import canonical_json, request_digest

MAX_FILES = 6500
MAX_TOTAL = 48_000_000
CONTEXT = {
    "config.json": ("config", 32000),
    "v1.json": ("v1_report", 32000),
    "plan.json": ("plan", 8000),
    "challenge.json": ("challenge", 16000),
    "trust.json": ("trust", 32000),
    "registry-issued.json": (None, 16000),
}
RECEIPT = r"(?:build|infrastructure)\.json|[A-J]/(?:before|after|fault|reviewer|host-[0-2])\.json"
BUNDLE = re.compile(
    rf"bundle/(?:{RECEIPT}|[A-J]/binding\.json|[A-J]/host-[0-2]/(?:header|closed|event-[0-9]{{4}})\.json)"
)


class CustodyFile(Frozen):
    path: str = Field(min_length=1, max_length=160)
    length: int = Field(strict=True, ge=0, le=2_100_000)
    sha256: Digest


class EvidenceCustodyManifest(Frozen):
    schema_version: Literal[1] = 1
    stage: Literal["prepared", "final"]
    challenge_digest: Digest
    environment_id: UUID
    batch_digest: Digest
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    collector_sha256: Digest
    trust_sha256: Digest
    evidence_protocol: Literal[3] = 3
    cases: tuple[str, ...] = tuple("ABCDEFGHIJ")
    registry_binding_digest: Digest
    prepared_manifest_digest: Digest | None = None
    files: tuple[CustodyFile, ...] = Field(max_length=MAX_FILES)
    bundle_content_digest: Digest


def digest(value):
    return request_digest(value.model_dump(mode="json"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    return json.loads(
        raw, object_pairs_hook=unique_fields, parse_constant=reject_constant
    )


def checked_path(path, *, directory=False):
    path = Path(path).absolute()
    # Check lexical ancestors before resolving: a junction must not disappear
    # through resolve(). Reparse points are rejected on Windows as well.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("CustodyFileTypeRejected")
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("CustodyFileTypeRejected")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("CustodyFileTypeRejected")
    return path


def fingerprint(path):
    s = checked_path(path).stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink


def read_file(path, maximum):
    path = checked_path(path)
    before = fingerprint(path)
    if before[2] > maximum:
        raise ValueError("CustodyBoundsExceeded")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2] or opened.st_nlink != 1:
            raise ValueError("CustodyInputChanged")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if (
        len(raw) > maximum
        or len(raw) != before[2]
        or before != fingerprint(path)
        or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    ):
        raise ValueError("CustodyInputChanged")
    return raw


def sync_dir(path):
    # Windows files are fsynced; Python offers no portable directory fsync there.
    # Atomic power-loss durability on Windows therefore needs operator storage.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_new(path, raw):
    path = Path(path)
    checked_path(path.parent, directory=True)
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def private_dir(path):
    if os.name == "nt" and sys.version_info < (3, 12, 4):
        raise ValueError("CustodyPrivateDirectoryRequired")
    path = checked_path(path, directory=True)
    if os.name != "nt" and (
        path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()
    ):
        raise ValueError("CustodyPrivateDirectoryRequired")
    return path


def directory_identity(path):
    p = private_dir(path)
    s = p.stat()
    return request_digest(
        (str(p), s.st_dev, s.st_ino, getattr(s, "st_birthtime_ns", None))
    )


def init_store(path):
    path = Path(path).absolute()
    checked_path(path.parent, directory=True)
    path.mkdir(mode=0o700, exist_ok=False)
    write_new(
        path / "store.json",
        canonical_json({"version": 1, "identity": directory_identity(path)}).encode(),
    )
    return path


def store_path(path):
    root = private_dir(path)
    if decode(read_file(root / "store.json", 1000)) != {
        "version": 1,
        "identity": directory_identity(root),
    }:
        raise ValueError("CustodyStoreIdentityMismatch")
    return root


def valid_name(name, stage):
    return (
        name in CONTEXT
        or BUNDLE.fullmatch(name)
        or (stage == "prepared" and re.fullmatch(rf"requests/(?:{RECEIPT})", name))
    )


def maximum(name):
    if name in CONTEXT:
        return CONTEXT[name][1]
    if "/host-" in name and not re.fullmatch(
        rf"(?:bundle|requests)/(?:{RECEIPT})", name
    ):
        return 16000
    return 8000 if name.endswith("/binding.json") else 2_100_000


def walk(root, prefix=""):
    root = checked_path(root, directory=True)
    found, visited = {}, 0

    def visit(folder):
        nonlocal visited
        with os.scandir(folder) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_FILES + 100:
                    raise ValueError("CustodyBoundsExceeded")
                p = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    checked_path(p, directory=True)
                    name = "/".join(
                        filter(None, (prefix, p.relative_to(root).as_posix()))
                    )
                    if name not in {"bundle", "requests"} and not re.fullmatch(
                        r"bundle/[A-J](?:/host-[0-2])?|requests/[A-J]", name
                    ):
                        raise ValueError("CustodyLayoutRejected")
                    visit(p)
                else:
                    found[p.relative_to(root).as_posix()] = checked_path(p)

    visit(root)
    return found


def capture(paths, stage):
    if len(paths) > MAX_FILES or any(not valid_name(n, stage) for n in paths):
        raise ValueError("CustodyLayoutRejected")
    before = {n: fingerprint(p) for n, p in paths.items()}
    data, total, journal_sizes = {}, 0, {}
    for name, path in sorted(paths.items()):
        raw = read_file(path, min(maximum(name), MAX_TOTAL - total))
        decode(raw)  # Reject duplicate fields/NaN even in unsigned inputs.
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError("CustodyBoundsExceeded")
        data[name] = raw
        if re.fullmatch(r"bundle/[A-J]/host-[0-2]/event-[0-9]{4}\.json", name):
            journal = name.rsplit("/", 1)[0]
            journal_sizes[journal] = journal_sizes.get(journal, 0) + len(raw)
            if journal_sizes[journal] > MAX_BYTES:
                raise ValueError("CustodyBoundsExceeded")
    if before != {n: fingerprint(p) for n, p in paths.items()}:
        raise ValueError("CustodyInputChanged")
    return data


def file_records(data):
    return tuple(
        CustodyFile(path=n, length=len(raw), sha256=sha(raw))
        for n, raw in sorted(data.items())
    )


def registry_record(challenge, registry):
    return {
        "challenge_digest": digest(challenge),
        "directory_identity": directory_identity(registry),
        "issued_sha256": sha(read_file(Path(registry) / "issued.json", 16000)),
    }


def bind_registry(store, challenge, registry, *, create=False):
    check_registration(registry, challenge)
    value = registry_record(challenge, registry)
    path = store / f"registry-{digest(challenge)}.json"
    raw = canonical_json(value).encode()
    if create:
        try:
            write_new(path, raw)
        except FileExistsError:
            pass
    if read_file(path, 2000) != raw:
        raise ValueError("CustodyRegistryMismatch")
    if (store / f"spent-{digest(challenge)}.json").exists():
        raise ValueError("ChallengeReplay")
    return request_digest(value)


@dataclass(frozen=True)
class Snapshot:
    store: Path
    manifest: EvidenceCustodyManifest
    data: dict[str, bytes]
    identities: dict | None = None

    def __post_init__(self):
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.identities is not None:
            object.__setattr__(
                self, "identities", MappingProxyType(dict(self.identities))
            )

    @property
    def root(self):
        return self.store / digest(self.manifest) / "files"

    def verify(self):
        other = load(self.store, digest(self.manifest))
        if other.data != self.data or (
            self.identities is not None and other.identities != self.identities
        ):
            raise ValueError("CustodySnapshotChanged")

    def arguments(self, args):
        values = vars(args).copy()
        values.update(
            {attr: str(self.root / name) for name, (attr, _) in CONTEXT.items() if attr}
        )
        values["bundle"] = str(self.root / "bundle")
        return SimpleNamespace(**values)


def load(store, manifest_digest, _ancestors=()):
    if manifest_digest in _ancestors or len(_ancestors) >= 4:
        raise ValueError("CustodyPreparedChainRejected")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_digest):
        raise ValueError("CustodyManifestRejected")
    store = store_path(store)
    root = private_dir(store / manifest_digest)
    if {p.name for p in root.iterdir()} != {"files", "manifest.json"}:
        raise ValueError("CustodyLayoutRejected")
    raw = read_file(root / "manifest.json", 2_000_000)
    manifest = EvidenceCustodyManifest.model_validate(decode(raw))
    if (
        digest(manifest) != manifest_digest
        or raw != canonical_json(manifest.model_dump(mode="json")).encode()
    ):
        raise ValueError("CustodyManifestRejected")
    paths = walk(root / "files")
    data = capture(paths, manifest.stage)
    records = file_records(data)
    if (
        records != manifest.files
        or request_digest([r.model_dump(mode="json") for r in records])
        != manifest.bundle_content_digest
        or manifest.cases != tuple("ABCDEFGHIJ")
    ):
        raise ValueError("CustodySnapshotChanged")
    if manifest.prepared_manifest_digest:
        parent = load(
            store, manifest.prepared_manifest_digest, (*_ancestors, manifest_digest)
        )
        if parent.manifest.stage != "prepared":
            raise ValueError("CustodyPreparedMismatch")
    return Snapshot(
        store, manifest, data, {name: fingerprint(path) for name, path in paths.items()}
    )


def check_requests(data, challenge, trust):
    now = datetime.now(timezone.utc)
    if not challenge.issued_at <= now <= challenge.expires_at:
        raise ValueError("EvidenceExpired")
    signers = {s.key_id: s for s in trust.signers}
    envelopes = {}
    for name, raw in data.items():
        if re.fullmatch(rf"bundle/(?:{RECEIPT})", name):
            envelopes[name.removeprefix("bundle/")] = (
                CausalSignedEvidence.model_validate(decode(raw)).evidence
            )
    for name, raw in data.items():
        if name.startswith("requests/"):
            envelopes[name.removeprefix("requests/")] = CausalEvidence.model_validate(
                decode(raw)
            )
    for name, raw in data.items():
        if not name.startswith("requests/"):
            continue
        evidence = CausalEvidence.model_validate(decode(raw))
        signer = signers.get(evidence.attestation_identity)
        if (
            evidence.challenge != challenge
            or signer is None
            or signer.role != evidence.payload.source
            or not signer.not_before
            <= evidence.started_at
            <= evidence.completed_at
            <= min(now, signer.not_after)
            or now > signer.not_after
            or (
                signer.role == "host"
                and signer.host_digest != evidence.payload.host_digest
            )
        ):
            raise ValueError("CustodySigningRequestRejected")
        check_receipt_name(name.removeprefix("requests/"), evidence)
        if signer.role == "host" and "config.json" in data:
            workers = [
                h["host_digest"]
                for h in decode(data["config.json"])["hosts"]
                if h["role"] == "worker"
            ]
            index = int(Path(name).stem.removeprefix("host-"))
            if index >= len(workers) or workers[index] != evidence.payload.host_digest:
                raise ValueError("CustodySigningRequestRejected")
        if signer.role == "reviewer":
            reviewed = [
                e
                for e in envelopes.values()
                if e.payload.source != "reviewer"
                and getattr(e.payload, "case_id", evidence.payload.case_id)
                == evidence.payload.case_id
            ]
            if (
                not reviewed
                or evidence.payload.reviewed_evidence_digest
                != evidence_set_digest(
                    [SimpleNamespace(evidence_digest=digest(e)) for e in reviewed]
                )
                or evidence.started_at < max(e.completed_at for e in reviewed)
            ):
                raise ValueError("CustodyReviewerRequestMismatch")


def check_receipt_name(name, evidence):
    source = evidence.payload.source
    case = getattr(evidence.payload, "case_id", None)
    expected = {
        "build": "build.json",
        "infrastructure": "infrastructure.json",
        "fault": f"{case}/fault.json",
        "reviewer": f"{case}/reviewer.json",
        "database": f"{case}/{getattr(evidence.payload, 'phase', '')}.json",
    }
    if (source == "host" and not re.fullmatch(rf"{case}/host-[0-2]\.json", name)) or (
        source != "host" and name != expected[source]
    ):
        raise ValueError("CustodyReceiptIdentityMismatch")


def check_returns(prepared, data, challenge, trust):
    receipts = []
    for name, raw in data.items():
        if name.startswith("requests/"):
            continue  # A later prepared snapshot may add exact Reviewer requests.
        if re.fullmatch(rf"bundle/(?:{RECEIPT})", name):
            receipt = CausalSignedEvidence.model_validate(decode(raw))
            check_receipt_name(name.removeprefix("bundle/"), receipt.evidence)
            request_name = "requests/" + name.removeprefix("bundle/")
            if request_name in prepared.data:
                expected = CausalEvidence.model_validate(
                    decode(prepared.data[request_name])
                )
                if signing_bytes(expected) != signing_bytes(receipt.evidence):
                    raise ValueError("CustodySigningReturnMismatch")
            elif name in prepared.data:
                if raw != prepared.data[name]:
                    raise ValueError("CustodySourceChanged")
            else:
                raise ValueError("CustodySigningRequestMissing")
            receipts.append(receipt)
        elif name not in prepared.data or raw != prepared.data[name]:
            raise ValueError("CustodySourceChanged")
    for name in prepared.data:
        if not name.startswith("requests/") and name not in data:
            raise ValueError("CustodySourceMissing")
    # Reuse the original role/host/key/time/signature/review/replay checks.
    verify_causal_acceptance(challenge, trust, receipts)


def freeze(args, store, *, stage, prepared_digest=None, requests=None):
    from verify_rc_acceptance import context, matches

    store = store_path(store)
    if stage not in {"prepared", "final"}:
        raise ValueError("CustodyStageRejected")
    paths = {
        name: Path(getattr(args, attr))
        if attr
        else Path(args.state_dir) / "issued.json"
        for name, (attr, _) in CONTEXT.items()
    }
    bundle_paths = walk(args.bundle, "bundle")
    paths.update({"bundle/" + n: p for n, p in bundle_paths.items()})
    request_paths = walk(requests, "requests") if requests else {}
    paths.update({"requests/" + n: p for n, p in request_paths.items()})
    data = capture(paths, stage)
    if bundle_paths != walk(args.bundle, "bundle") or (
        requests and request_paths != walk(requests, "requests")
    ):
        raise ValueError("CustodyInputChanged")
    challenge = CausalChallenge.model_validate(decode(data["challenge.json"]))
    if (
        CausalChallenge.model_validate(decode(data["registry-issued.json"]))
        != challenge
    ):
        raise ValueError("CustodyRegistryMismatch")
    if sha(data["trust.json"]) != args.trust_sha256:
        raise ValueError("TrustPinMismatch")
    trust = AcceptanceTrust.model_validate(decode(data["trust.json"]))
    check_requests(data, challenge, trust)
    registry_binding = bind_registry(store, challenge, args.state_dir, create=True)
    if registry_record(challenge, args.state_dir)["issued_sha256"] != sha(
        data["registry-issued.json"]
    ):
        raise ValueError("CustodyRegistryMismatch")
    if prepared_digest:
        prepared = load(store, prepared_digest)
        if (
            prepared.manifest.stage != "prepared"
            or prepared.manifest.registry_binding_digest != registry_binding
        ):
            raise ValueError("CustodyPreparedMismatch")
        check_returns(prepared, data, challenge, trust)
    records = file_records(data)
    manifest = EvidenceCustodyManifest(
        stage=stage,
        challenge_digest=digest(challenge),
        environment_id=challenge.environment_id,
        batch_digest=challenge.staging_batch_digest,
        deployment_sha=challenge.deployment_sha,
        collector_sha256=args.collector_sha256,
        trust_sha256=args.trust_sha256,
        registry_binding_digest=registry_binding,
        prepared_manifest_digest=prepared_digest,
        files=records,
        bundle_content_digest=request_digest(
            [r.model_dump(mode="json") for r in records]
        ),
    )
    root = store / digest(manifest)
    root.mkdir(mode=0o700, exist_ok=False)
    (root / "files").mkdir(mode=0o700)
    (root / "files" / "bundle").mkdir(mode=0o700)
    for name, raw in data.items():
        target = root / "files" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_new(target, raw)
    directories = {root / "files"}
    for name in data:
        parent = (root / "files" / name).parent
        while parent != root:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        sync_dir(directory)
    # Validate the COPIED context, never reopen mutable source context.
    snap = Snapshot(store, manifest, data)
    with frozen_input_bytes(snap.root, snap.data):
        config, original, _, batch = context(snap.arguments(args))
    matches(challenge, config, original, batch)
    write_new(
        root / "manifest.json",
        canonical_json(manifest.model_dump(mode="json")).encode(),
    )
    snap.verify()
    sync_dir(root)
    sync_dir(store)
    return manifest


def run_release(args):
    from verify_rc_release import (
        CASES,
        CustodyReleaseReport,
        ReleaseCase,
        consume_challenge,
        evaluate,
    )

    snapshot = None
    try:
        snapshot = load(args.custody_store, args.manifest_sha256)
        manifest = snapshot.manifest
        if manifest.stage != "final" or (
            manifest.trust_sha256,
            manifest.collector_sha256,
        ) != (args.trust_sha256, args.collector_sha256):
            raise ValueError("CustodyContextMismatch")
        challenge = CausalChallenge.model_validate(
            decode(snapshot.data["challenge.json"])
        )
        if (
            manifest.challenge_digest != digest(challenge)
            or manifest.environment_id != challenge.environment_id
            or manifest.batch_digest != challenge.staging_batch_digest
            or manifest.deployment_sha != challenge.deployment_sha
        ):
            raise ValueError("CustodyContextMismatch")
        if (
            bind_registry(snapshot.store, challenge, args.state_dir)
            != manifest.registry_binding_digest
        ):
            raise ValueError("CustodyRegistryMismatch")
        with frozen_input_bytes(snapshot.root, snapshot.data):
            report = evaluate(snapshot.arguments(args))
        if manifest.prepared_manifest_digest:
            prepared = load(snapshot.store, manifest.prepared_manifest_digest)
            check_returns(
                prepared,
                snapshot.data,
                challenge,
                AcceptanceTrust.model_validate(decode(snapshot.data["trust.json"])),
            )
        snapshot.verify()
        if args.command == "custody-verify" and not report.errors:
            if (
                bind_registry(snapshot.store, challenge, args.state_dir)
                != manifest.registry_binding_digest
            ):
                raise ValueError("CustodyRegistryMismatch")
            # Durable intent outside the registry prevents clearing/copying the
            # registry from replaying within this protected custody domain.
            write_new(
                snapshot.store / f"spent-{digest(challenge)}.json",
                canonical_json(
                    {
                        "manifest_digest": digest(manifest),
                        "challenge_digest": digest(challenge),
                    }
                ).encode(),
            )
            consume_challenge(args.state_dir, challenge)
            if (
                read_file(Path(args.state_dir) / "consumed.json", 64)
                != digest(challenge).encode()
            ):
                raise ValueError("CustodyRegistryMismatch")
            if (
                request_digest(registry_record(challenge, args.state_dir))
                != manifest.registry_binding_digest
            ):
                raise ValueError("CustodyRegistryMismatch")
            verify_causal_acceptance(
                challenge,
                AcceptanceTrust.model_validate(decode(snapshot.data["trust.json"])),
                [],
            )
            snapshot.verify()  # Tampering even inside a controlled consume hook rejects.
        result = CustodyReleaseReport(
            **report.model_dump(exclude={"schema_version"}),
            custody_validation="PASS",
            manifest_digest=digest(manifest),
            bundle_content_digest=manifest.bundle_content_digest,
            prepared_manifest_digest=manifest.prepared_manifest_digest,
        )
        return (
            0
            if result.saved_evidence_validation == "PASS"
            else 2
            if result.saved_evidence_validation == "FAIL"
            else 1
        ), result
    except Exception:
        return 2, CustodyReleaseReport(
            status="FAIL",
            software_validation="FAIL",
            saved_evidence_validation="FAIL",
            custody_validation="FAIL",
            manifest_digest=digest(snapshot.manifest) if snapshot else None,
            bundle_content_digest=snapshot.manifest.bundle_content_digest
            if snapshot
            else None,
            cases=[ReleaseCase(case_id=c) for c in CASES],
            errors=("CustodyVerificationRejected",),
        )


def handoff(store, manifest_digest, receipt_path):
    snapshot = load(store, manifest_digest)
    if snapshot.manifest.stage != "prepared" or not re.fullmatch(RECEIPT, receipt_path):
        raise ValueError("CustodySigningRequestRejected")
    data = snapshot.data
    name = "requests/" + receipt_path
    if name in data:
        evidence = CausalEvidence.model_validate(decode(data[name]))
    else:
        evidence = CausalSignedEvidence.model_validate(
            decode(data["bundle/" + receipt_path])
        ).evidence
    challenge = CausalChallenge.model_validate(decode(data["challenge.json"]))
    trust = AcceptanceTrust.model_validate(decode(data["trust.json"]))
    check_requests(
        {**data, name: canonical_json(evidence.model_dump(mode="json")).encode()},
        challenge,
        trust,
    )
    check_receipt_name(receipt_path, evidence)
    case = getattr(evidence.payload, "case_id", None)
    binding = data.get(f"bundle/{case}/binding.json")
    result = {
        "schema_version": 1,
        "status": "UNSIGNED",
        "role": evidence.payload.source,
        "evidence": evidence.model_dump(mode="json"),
        "payload_digest": request_digest(evidence.payload.model_dump(mode="json")),
        "evidence_digest": digest(evidence),
        "signing_domain": "Altas.RCCausalEvidence.v3\\n",
        "signing_bytes_base64": base64.b64encode(signing_bytes(evidence)).decode(),
        "prepared_manifest_digest": digest(snapshot.manifest),
        "source_files": [r.model_dump(mode="json") for r in snapshot.manifest.files],
        "binding_digest": request_digest(decode(binding)) if binding else None,
        "deployment_identity": decode(data["config.json"])["expected_identity"],
        "manual_checks": [
            "ProtectedSourceAndProcess",
            "IndependentFaultConfirmation",
            "PhysicalAssetInventory",
            "ExactReviewerEvidenceSet",
        ],
    }
    snapshot.verify()
    return {**result, "request_digest": request_digest(result)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-store", "freeze", "handoff"))
    parser.add_argument("--store", required=True)
    parser.add_argument("--stage", choices=("prepared", "final"))
    parser.add_argument("--prepared-manifest-sha256")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--receipt-path")
    parser.add_argument("--requests")
    for name in (
        "config",
        "v1-report",
        "plan",
        "challenge",
        "state-dir",
        "trust",
        "trust-sha256",
        "collector-sha256",
        "bundle",
    ):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    try:
        if args.command == "init-store":
            init_store(args.store)
            result = {"status": "PRIVATE_STORE_CREATED", "schema_version": 1}
        elif args.command == "freeze":
            manifest = freeze(
                args,
                args.store,
                stage=args.stage,
                prepared_digest=args.prepared_manifest_sha256,
                requests=args.requests,
            )
            result = {
                "status": "FROZEN_COPY",
                "manifest_digest": digest(manifest),
                "manifest": manifest.model_dump(mode="json"),
            }
        else:
            result = handoff(args.store, args.manifest_sha256, args.receipt_path)
        print(canonical_json(result))
        return 0
    except Exception:
        print(canonical_json({"status": "FAIL", "error": "CustodyOperationRejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
