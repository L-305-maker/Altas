"""Install the wheel outside the checkout and exercise the reference application."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SMOKE = """
import asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from agentflow.application.orchestration.worker import Worker
from agentflow.config import Settings
from agentflow.infrastructure.persistence.store import Store
from applications.living_guideline.bootstrap import build_handlers, create_app

settings = Settings(provider="mock", api_token=SecretStr("wheel-smoke-test-token"))
store = Store("sqlite:///smoke.db")
store.create_schema()
try:
    with TestClient(create_app(settings, store)) as client:
        client.headers["Authorization"] = "Bearer wheel-smoke-test-token"
        response = client.post("/api/guidelines", json={"title": "wheel", "documents": [{"title": "doc", "text": "evidence"}]})
        assert response.status_code == 201
        run_id = response.json()["id"]
        worker = Worker(store, build_handlers(settings))
        asyncio.run(worker.tick())
        asyncio.run(worker.tick())
        approval = store.approvals(run_id)[0]["id"]
        assert client.post(f"/api/approvals/{approval}", json={"approve": True, "actor": "wheel-test"}).status_code == 200
        asyncio.run(worker.tick())
        assert store.get_run(run_id)["status"] == "succeeded"
        assert store.artifacts(run_id)[0]["content"]["citations"][0]["quote"] == "evidence"
finally:
    store.close()
from applications.living_guideline.evaluation import main
asyncio.run(main())
print("Installed wheel: reference application flow passed")
"""


def main():
    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="agentflow-wheel-") as directory:
        root = Path(directory)
        venv = root / "venv"
        subprocess.run(
            ["uv", "venv", str(venv), "--python", sys.executable], check=True
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)], check=True
        )
        subprocess.run([str(python), "-I", "-c", SMOKE], cwd=root, check=True)


if __name__ == "__main__":
    main()
