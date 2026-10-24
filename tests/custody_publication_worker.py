"""Abrupt exits of owned test children, with no private signing keys on disk."""

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace


def run_worker(input_file, phase, trace_file, barrier=None):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import rc_evidence_custody as custody

    params = json.loads(Path(input_file).read_bytes())
    args = SimpleNamespace(**params["args"])
    store, trace_file = Path(params["store"]), Path(trace_file)
    trace = {}
    actual_digest, actual_mkdir, actual_write = (
        custody.digest,
        Path.mkdir,
        custody.write_new,
    )
    has_atomic = hasattr(custody, "publish_no_replace")

    def record():
        trace_file.write_text(json.dumps(trace), encoding="utf-8")

    def stop():
        record()
        os._exit(73)

    def digest(value):
        result = actual_digest(value)
        if isinstance(value, custody.EvidenceCustodyManifest):
            trace["manifest_digest"] = result
        return result

    def mkdir(path, *values, **kwargs):
        is_object = path.parent == store and (
            re.fullmatch(r"[a-f0-9]{64}", path.name)
            or path.name.startswith(".pending-")
        )
        if is_object and barrier is not None and not has_atomic:
            barrier.wait(timeout=30)
        result = actual_mkdir(path, *values, **kwargs)
        if is_object:
            trace["work_name"] = path.name
            if phase == "allocated":
                stop()
        return result

    def write(path, raw):
        path = Path(path)
        work = store / trace.get("work_name", "unallocated")
        if path == work / "manifest.json" and phase == "before_manifest":
            stop()
        result = actual_write(path, raw)
        if path.is_relative_to(work / "files") and phase == "partial":
            stop()
        if path == work / "manifest.json" and phase == "manifest_written":
            stop()
        return result

    custody.digest, custody.write_new, Path.mkdir = digest, write, mkdir
    if has_atomic and barrier is not None:
        original_publish = custody.publish_no_replace

        def publish(source, destination):
            barrier.wait(timeout=30)
            return original_publish(source, destination)

        custody.publish_no_replace = publish
    try:
        manifest = custody.freeze(args, store, stage="final")
        trace.update(manifest_digest=actual_digest(manifest), completed=True)
        record()
    except Exception as error:
        trace.update(completed=False, error_type=type(error).__name__)
        record()
        raise SystemExit(74) from None
