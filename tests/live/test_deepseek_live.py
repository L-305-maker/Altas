"""Paid acceptance is disabled unless explicitly authorized by the operator."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.live_provider
def test_real_https_deepseek_to_durable_artifact():
    script = Path(__file__).resolve().parents[2] / "scripts/verify_live_provider.py"
    result = subprocess.run(
        [sys.executable, str(script), "--allow-paid-request"],
        capture_output=True,
        text=True,
        timeout=80,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    # Script emits only a fixed safe report, never exception bodies or credentials.
    assert result.returncode == 0, "LiveProviderAcceptanceFailed"
    report = json.loads(result.stdout)
    assert report["status"] == "PASS" and report["requests"] == 1
