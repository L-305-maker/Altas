"""Black-box installed-wheel campaign; all Core imports use the public facade."""

import hashlib
import json
import os
import runpy
import site
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentflow.runtime import Store, WorkflowSpec


def verify():
    import agentflow

    installed = Path(agentflow.__file__).resolve()
    assert any(
        installed.is_relative_to(Path(p).resolve()) for p in site.getsitepackages()
    )
    assert not any(name.startswith("applications.") for name in sys.modules)
    root = Path(__file__).resolve().parent
    consumer = runpy.run_path(str(root / "consumer.py"))
    store, agents, handlers = consumer["components"]()
    ledger = Path(os.environ["INDEPENDENT_CONSUMER_RECORDS"]) / "effects.jsonl"

    def effects():
        return (
            [json.loads(line) for line in ledger.read_bytes().splitlines()]
            if ledger.exists()
            else []
        )

    def worker(run_id, exitcode=0):
        result = subprocess.run(
            [sys.executable, "-I", str(root / "consumer.py"), run_id],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode == exitcode, "IndependentWorkerExitMismatch"

    try:
        with TestClient(consumer["api"](store, handlers)) as client:
            assert client.get("/api/meta").status_code == 401
            client.headers["Authorization"] = (
                "Bearer " + os.environ["INDEPENDENT_CONSUMER_API_TOKEN"]
            )
            assert client.get("/health").json() == {"status": "ok"}
            spec = consumer["workflow"]()
            definition = spec.model_dump(mode="json")
            response = client.post("/api/workflows", json=definition)
            assert response.status_code == 201
            workflow_id = response.json()["id"]
            assert (
                client.post("/api/workflows", json=definition).json()["id"]
                == workflow_id
            )
            incompatible = json.loads(json.dumps(definition))
            incompatible["steps"][1]["parameters"] = {}
            assert client.post("/api/workflows", json=incompatible).status_code == 409
            incompatible["version"] = 2
            assert client.post("/api/workflows", json=incompatible).status_code == 201

            def submit(key, crash=False):
                body = {
                    "workflow_id": workflow_id,
                    "inputs": {"record": key, "crash_after_dispatch": crash},
                    "submission_key": key,
                }
                first = client.post("/api/runs", json=body)
                assert first.status_code == 201
                assert client.post("/api/runs", json=body).json() == first.json()
                assert (
                    client.post(
                        "/api/runs", json={**body, "inputs": {"changed": True}}
                    ).status_code
                    == 409
                )
                run_id = first.json()["id"]
                assert client.get(f"/api/runs/{run_id}").json()["spec"] == definition
                return run_id

            def approve(run_id):
                approvals = client.get(
                    "/api/agent/action-approvals", params={"run_id": run_id}
                ).json()
                assert len(approvals) == 1 and approvals[0]["consumed_at"] is None
                approval = approvals[0]
                history = client.get(
                    f"/api/agent/sessions/{approval['session_id']}/reviews"
                ).json()
                response = client.post(
                    f"/api/agent/action-approvals/{approval['approval_id']}/decision",
                    json={
                        "expected_revision": history["revision"],
                        "decision": "approved",
                        "actor": "independent-operator",
                    },
                )
                assert response.status_code == 200
                return approval

            occupant = submit("capacity-occupant")
            normal = submit("normal")
            assert client.get("/api/runtime/queue").json()["active_runs"] == 2
            rejected = client.post(
                "/api/runs",
                json={
                    "workflow_id": workflow_id,
                    "inputs": {},
                    "submission_key": "over-limit",
                },
            )
            assert rejected.status_code == 429 and "retry-after" not in rejected.headers
            assert rejected.json() == {"detail": "WorkflowCapacityExceeded"}
            # submit() has already checked identical replay and conflicting identity at capacity.
            assert submit("normal") == normal
            assert client.post(f"/api/runs/{occupant}/cancel").status_code == 200
            assert client.get("/api/runtime/queue").json()["active_runs"] == 1
            worker(normal)  # A real process exits waiting for explicit Action Approval.
            assert not effects()
            approval = approve(normal)
            worker(normal)  # A new process resumes the stored session.
            assert store.get_run(normal)["status"] == "succeeded"
            artifacts = store.artifacts(normal)
            assert len(artifacts) == 2
            assert all(
                hashlib.sha256(
                    json.dumps(
                        a["content"],
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                == a["digest"]
                for a in artifacts
            )
            assert any(a["content"].get("consumer") == "independent" for a in artifacts)
            assert len(effects()) == 1
            saved = agents.get_action_approval(approval["approval_id"])
            assert saved.consumed_at is not None
            worker(normal)
            assert len(effects()) == 1 and store.artifacts(normal) == artifacts

            unknown = submit("unknown", crash=True)
            worker(unknown)
            unknown_approval = approve(unknown)
            worker(unknown, 73)  # Dies AFTER the mock effect, BEFORE confirmation.
            assert len(effects()) == 2
            worker(unknown)  # Waits for the real PostgreSQL lease to expire.
            pending = client.get(
                "/api/agent/reconciliations/pending", params={"run_id": unknown}
            ).json()
            assert (
                len(pending) == 1
                and pending[0]["agent_status"] == "waiting_reconciliation"
            )
            action = pending[0]
            assert action["action_id"] == unknown_approval["action_id"]
            assert effects()[-1] == {
                "action": action["action_id"],
                "key": action["idempotency_key"],
                "record": "unknown",
            }
            worker(unknown)
            assert len(effects()) == 2  # UNKNOWN is never dispatched automatically.
            response = client.post(
                f"/api/agent/sessions/{action['session_id']}/actions/{action['action_id']}/reconciliations",
                json={
                    "request_id": "independent-reconcile",
                    "expected_revision": action["revision"],
                    "confirmed_status": "succeeded",
                    "result": {"recorded": True},
                    "evidence_reference": "owned-mock-ledger",
                    "actor": "independent-operator",
                },
            )
            assert response.status_code == 200 and response.json()["applied"]
            worker(unknown)
            assert (
                store.get_run(unknown)["status"] == "succeeded" and len(effects()) == 2
            )

            # Public Store boundary: actual DB-time lease expiry, then cancellation.
            controlled = submit("fencing-cancel")
            old = store.claim(0.1)
            assert old and old.run_id == controlled
            peer = Store(os.environ["INDEPENDENT_CONSUMER_DB_URL"])
            try:
                deadline, new = time.monotonic() + 5, None
                while new is None and time.monotonic() < deadline:
                    new = peer.claim(3)
                    if new is None:
                        time.sleep(0.02)
                assert new and new.step_id == old.step_id and new.token != old.token
                assert (
                    new.attempt == old.attempt + 1
                    and new.idempotency_key == old.idempotency_key
                )
                assert store.complete(old, {"late": True}) is False
                assert store.heartbeat(old, 3) is False
                assert client.post(f"/api/runs/{controlled}/cancel").status_code == 200
                assert peer.complete(new, {"cancelled_late": True}) is False
            finally:
                peer.close()
            worker(controlled)
            assert (
                store.get_run(controlled)["status"] == "cancelled"
                and len(effects()) == 2
            )
            assert not store.artifacts(controlled)

            # Cancel an actual Durable session while its Action awaits approval.
            cancelled_agent = submit("cancel-pending-agent")
            worker(cancelled_agent)
            approvals = client.get(
                "/api/agent/action-approvals", params={"run_id": cancelled_agent}
            ).json()
            assert len(approvals) == 1 and not approvals[0]["consumed_at"]
            assert client.post(f"/api/runs/{cancelled_agent}/cancel").status_code == 200
            worker(cancelled_agent)
            assert store.get_run(cancelled_agent)["status"] == "cancelled"
            assert len(effects()) == 2 and not store.artifacts(cancelled_agent)
            assert store.get_run(normal)["spec"] == definition
            assert (
                WorkflowSpec.model_validate(definition)
                .steps[1]
                .parameters["agent"]["mode"]
                == "durable_v1"
            )
            assert not any(name.startswith("applications.") for name in sys.modules)
        return {
            "status": "PASS",
            "installed_wheel": True,
            "backend": "postgresql",
            "dag_runs": 5,
            "max_active_runs": 2,
            "capacity_rejection": "verified",
            "full_capacity_idempotency": "verified",
            "mock_effects": 2,
            "approved_actions": 2,
            "unknown_redispatches": 0,
            "cancelled_late_write": "rejected",
            "cancelled_agent_dispatches": 0,
            "stale_lease_write": "rejected",
            "snapshot_immutable": True,
            "legacy_fallback": False,
            "physical_exactly_once": "NOT_VERIFIED",
        }
    finally:
        store.close()


if __name__ == "__main__":
    try:
        print(json.dumps(verify()))
    except Exception as error:
        trace = error.__traceback__
        while trace.tb_next:
            trace = trace.tb_next
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": "IndependentConsumerRejected",
                    "check_line": trace.tb_lineno,
                }
            )
        )
        raise SystemExit(2) from None
