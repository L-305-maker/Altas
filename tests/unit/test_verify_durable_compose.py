import importlib.util
from pathlib import Path


def test_compose_verifier_exercises_durable_mode(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_compose.py"
    spec = importlib.util.spec_from_file_location("verify_compose", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    submitted = []

    def api(path, method, payload):
        submitted.append((path, payload))
        return {"id": "workflow" if path == "/workflows" else "run"}

    monkeypatch.setattr(verifier, "api", api)
    monkeypatch.setattr(
        verifier,
        "wait_for_status",
        lambda *args: {
            "steps": [{"output": {"text": "done", "rounds": 1}}],
            "artifacts": [{"content": {"text": "done", "rounds": 1}}],
        },
    )
    verifier.verify_durable_agent()
    step = submitted[0][1]["steps"][0]
    assert step["handler"] == "agent"
    assert step["parameters"]["agent"]["mode"] == "durable_v1"
    assert submitted[1][1]["workflow_id"] == "workflow"
