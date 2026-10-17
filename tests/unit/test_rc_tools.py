import importlib.util
import json
import sys
from pathlib import Path

import pytest


def script(monkeypatch, name):
    root = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_rc_database_creation_rejects_external_names(monkeypatch):
    module = script(monkeypatch, "rc_support")
    with pytest.raises(ValueError, match="isolated"):
        module.TemporaryPostgres("unused").database("production")


@pytest.mark.parametrize("seconds,workload", [(0, 1), (3601, 1), (1, 0), (1, 1001)])
def test_soak_rejects_unbounded_work(monkeypatch, seconds, workload):
    module = script(monkeypatch, "verify_durable_soak")
    with pytest.raises(ValueError):
        module.validate_limits(seconds, workload)


def test_live_requires_both_explicit_payment_authorization_and_key(monkeypatch):
    module = script(monkeypatch, "verify_live_provider")
    monkeypatch.delenv("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", raising=False)
    monkeypatch.setenv("AGENTFLOW_DEEPSEEK_API_KEY", "must-never-be-used")
    assert module.authorized() is False
    monkeypatch.setenv("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", "one-paid-request")
    monkeypatch.delenv("AGENTFLOW_DEEPSEEK_API_KEY")
    assert module.authorized() is False


@pytest.mark.asyncio
async def test_live_harness_is_one_request_and_completes_real_durable_step(
    monkeypatch, tmp_path
):
    module = script(monkeypatch, "verify_live_provider")
    from agentflow.infrastructure.models.mock import MockProvider

    provider = MockProvider()
    report = await module.verify(
        provider, f"sqlite:///{(tmp_path / 'live.db').as_posix()}"
    )
    assert report["status"] == "PASS" and report["requests"] == 1
    assert provider.calls == 1


def test_live_failure_report_never_prints_credentials(monkeypatch, capsys):
    module = script(monkeypatch, "verify_live_provider")
    private = "private-test-key-do-not-print"
    monkeypatch.setenv("AGENTFLOW_DEEPSEEK_API_KEY", private)
    monkeypatch.setenv("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", "one-paid-request")
    monkeypatch.setattr(
        sys, "argv", ["verify_live_provider.py", "--allow-paid-request"]
    )

    def broken(*args):
        raise RuntimeError(private)

    monkeypatch.setattr(module, "DeepSeekProvider", broken)
    assert module.main() == 1
    output = capsys.readouterr()
    assert private not in output.out + output.err
    assert "LiveProviderAcceptanceFailed" in output.out


def test_partial_pg_start_still_stops_owned_cluster(monkeypatch):
    module = script(monkeypatch, "rc_support")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == "start":
            root = Path(command[command.index("-D") + 1])
            root.mkdir()
            (root / "postmaster.pid").touch()
            raise RuntimeError("SyntheticStartFailure")

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(RuntimeError, match="SyntheticStartFailure"):
        with module.TemporaryPostgres("unused"):
            pytest.fail("partial start cannot yield a usable database")
    assert calls[-1][-1] == "stop"


def test_soak_fence_log_does_not_serialize_task_payload(monkeypatch):
    from agentflow.domain.execution.task import Task
    from agentflow.domain.workflow.spec import StepSpec

    module = script(monkeypatch, "rc_workload")
    task = Task(
        "test-run",
        "agent",
        "synthetic-token",
        1,
        StepSpec(id="agent", handler="rc-agent"),
        {"credential": "PRIVATE_INPUT_NEVER_LOG"},
        {"peer": {"document": "PRIVATE_DEPENDENCY_NEVER_LOG"}},
    )
    record = module.task_fence_record(task)
    assert record == {
        "run_id": "test-run",
        "step_id": "agent",
        "token": "synthetic-token",
        "attempt": 1,
    }
    assert "PRIVATE" not in json.dumps(record)


def test_live_pytest_does_not_run_on_environment_variables_alone():
    import os
    import subprocess

    env = dict(
        os.environ,
        AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION="one-paid-request",
        AGENTFLOW_DEEPSEEK_API_KEY="synthetic-test-key-never-use",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "tests/live/test_deepseek_live.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert "1 skipped" in completed.stdout
    assert (
        "paid provider tests require explicit --run-live-provider" in completed.stdout
    )
    assert env["AGENTFLOW_DEEPSEEK_API_KEY"] not in (
        completed.stdout + completed.stderr
    )
