"""Verify the running Docker Compose production baseline end to end."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = os.environ.get(
    "AGENTFLOW_VERIFY_BASE_URL", "http://127.0.0.1:3000/api"
).rstrip("/")
TOKEN = os.environ.get("AGENTFLOW_API_TOKEN", "")
TIMEOUT = float(os.environ.get("AGENTFLOW_VERIFY_TIMEOUT", "90"))


def api(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {path} returned {error.code}: {detail[:500]}"
        ) from None
    except URLError as error:
        raise RuntimeError(f"{method} {path} failed: {error.reason}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"{method} {path} returned non-JSON data") from None


def wait_for_api() -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            meta = api("/meta")
            if isinstance(meta, dict) and meta.get("version") == "1.0.0":
                return meta
        except RuntimeError as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError(f"AgentFlow did not become ready: {last_error}")


def wait_for_status(run_id: str, expected: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    terminal = {"succeeded", "failed", "cancelled"}
    while time.monotonic() < deadline:
        detail = api(f"/runs/{run_id}")
        status = detail.get("status")
        if status in expected:
            return detail
        if status in terminal:
            raise RuntimeError(
                f"run {run_id} reached {status!r}, expected {sorted(expected)}"
            )
        time.sleep(0.5)
    raise RuntimeError(
        f"run {run_id} did not reach {sorted(expected)} within {TIMEOUT:g}s"
    )


def main() -> None:
    if len(TOKEN) < 16:
        raise SystemExit("AGENTFLOW_API_TOKEN must contain at least 16 characters")

    meta = wait_for_api()
    source_text = "Compose smoke evidence: every published quote must come from this text."
    created = api(
        "/guidelines",
        "POST",
        {
            "title": "Compose v1.0 smoke test",
            "documents": [{"title": "smoke.txt", "text": source_text}],
        },
        {"Idempotency-Key": f"compose-smoke-{uuid4()}"},
    )
    run_id = created["id"]

    waiting = wait_for_status(run_id, {"waiting"})
    pending = [
        item
        for item in waiting.get("approvals", [])
        if item.get("decision") == "pending"
    ]
    if len(pending) != 1:
        raise RuntimeError(f"expected one pending approval, got {len(pending)}")

    api(
        f"/approvals/{pending[0]['id']}",
        "POST",
        {"approve": True, "actor": "compose-smoke", "reason": "release verification"},
    )
    final = wait_for_status(run_id, {"succeeded"})
    artifacts = final.get("artifacts", [])
    if len(artifacts) != 1:
        raise RuntimeError(f"expected one artifact, got {len(artifacts)}")
    content = artifacts[0].get("content")
    if not isinstance(content, dict) or content.get("status") != "approved":
        raise RuntimeError("published artifact is missing approved status")
    citations = content.get("citations")
    if not isinstance(citations, list) or not citations:
        raise RuntimeError("published artifact contains no citations")
    if any(
        not isinstance(item, dict) or item.get("quote") not in source_text
        for item in citations
    ):
        raise RuntimeError("published artifact contains an untraceable quote")

    print(
        json.dumps(
            {
                "version": meta["version"],
                "run_id": run_id,
                "status": final["status"],
                "artifact_digest": artifacts[0]["digest"],
                "traceable_citations": len(citations),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
