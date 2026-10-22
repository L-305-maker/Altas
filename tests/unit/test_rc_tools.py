import importlib.util
import json
import os
import subprocess
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


def test_postgres_gate_keeps_every_pg_variant_and_common_case(monkeypatch, tmp_path):
    module = script(monkeypatch, "verify_postgres")
    fixture = tmp_path / "test_backend_selection.py"
    fixture.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])\n"
        "def test_backend(backend): pass\n"
        "@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])\n"
        "def test_sqlite_comparison(backend): pass\n"
        "def test_shared(): pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(fixture),
            "-q",
            "-k",
            module.POSTGRES_SELECTION,
        ],
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, "PostgresGateSelectionFailed"
    assert "3 passed, 2 deselected" in result.stdout


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


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_live_cli_allowlists_current_model_with_mock_only(monkeypatch, capsys, model):
    module = script(monkeypatch, "verify_live_provider")
    from agentflow.infrastructure.models.mock import MockProvider

    credential = "synthetic-credential-never-send"
    providers = []

    def offline(key, selected_model):
        if key != credential or selected_model != model:
            pytest.fail("LiveConfigMismatch", pytrace=False)
        provider = MockProvider(model=selected_model)
        providers.append(provider)
        return provider

    monkeypatch.setattr(module, "DeepSeekProvider", offline)
    monkeypatch.setenv("AGENTFLOW_DEEPSEEK_API_KEY", credential)
    monkeypatch.setenv("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", "one-paid-request")
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_live_provider.py", "--allow-paid-request", "--model", model],
    )
    assert module.main() == 0
    output = capsys.readouterr()
    if credential in output.out + output.err:
        pytest.fail("LiveDiagnosticContainsCredential", pytrace=False)
    assert json.loads(output.out)["requests"] == providers[0].calls == 1


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


@pytest.mark.asyncio
async def test_worker_diagnostics_exclude_private_task_data(
    monkeypatch, tmp_path, capsys
):
    from agentflow.domain.execution.task import Task
    from agentflow.domain.workflow.spec import StepSpec

    module = script(monkeypatch, "rc_workload")
    task = Task(
        "fixture-run",
        "agent",
        "fixture-fence-token",
        1,
        StepSpec(
            id="agent", handler="rc-agent", parameters={"prompt": "PRIVATE_PROMPT"}
        ),
        {"scenario": "final", "document": "PRIVATE_INPUT", "key": "PRIVATE_SECRET"},
        {"before": "PRIVATE_DEPENDENCY"},
    )

    class FakeStore:
        def close(self):
            pass

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            pass

    class FakeWorker:
        def __init__(self, store, handlers, **kwargs):
            self.handlers = handlers

        async def tick(self):
            await self.handlers.get("rc-agent")(task)
            raise TimeoutError

    monkeypatch.setattr(module, "Store", lambda url: FakeStore())
    monkeypatch.setattr(module, "SQLAgentStore", lambda store, **kwargs: store)
    monkeypatch.setattr(module, "DurableAgentRunner", FakeRunner)
    monkeypatch.setattr(module, "Worker", FakeWorker)
    await module.serve("unused", tmp_path, 1)
    paths = list(tmp_path.rglob("worker-*.jsonl"))
    assert len(paths) == 1
    raw = paths[0].read_text(encoding="utf-8")
    output = capsys.readouterr()
    if "PRIVATE" in raw + output.out + output.err:
        pytest.fail("SoakDiagnosticContainsPrivateTaskData", pytrace=False)
    assert json.loads(raw)["task"] == {
        "run_id": task.run_id,
        "step_id": task.step_id,
        "token": task.token,
        "attempt": task.attempt,
    }
    assert paths[0].parent != tmp_path
    if os.name != "nt":
        assert paths[0].parent.stat().st_mode & 0o077 == 0
        assert paths[0].stat().st_mode & 0o077 == 0
    assert task.token not in output.out + output.err


# The child intercepts the paid script before it can start; even a broken gate
# can only fail this test, never send a real request with the synthetic key.
LIVE_GATE_PROGRAM = """
import asyncio, json, os, subprocess, sys
from pathlib import Path
import pytest
original = subprocess.run
def guarded(command, *args, **kwargs):
    if any(Path(str(part)).name == 'verify_live_provider.py' for part in command):
        if os.environ['RC_GATE_MODE'] != 'mock':
            raise AssertionError('PaidProviderSpawnForbidden')
        sys.path.insert(0, str(Path.cwd() / 'scripts'))
        from verify_live_provider import verify
        from agentflow.infrastructure.models.mock import MockProvider
        provider = MockProvider()
        report = asyncio.run(verify(provider, os.environ['RC_GATE_DB']))
        assert provider.calls == 1
        Path(os.environ['RC_GATE_MARKER']).write_text('mock-only')
        return subprocess.CompletedProcess(command, 0, json.dumps(report), '')
    return original(command, *args, **kwargs)
subprocess.run = guarded
raise SystemExit(pytest.main(sys.argv[1:]))
"""

MOCK_API_KEY = "synthetic-test-key-never-send"


@pytest.mark.parametrize(
    "option,authorization,key",
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_live_pytest_requires_option_authorization_and_key(
    tmp_path, option, authorization, key
):
    synthetic_key = MOCK_API_KEY
    marker = tmp_path / "mock-invoked"
    enabled = option and authorization and key
    environment = dict(
        os.environ,
        RC_GATE_MODE="mock" if enabled else "block",
        RC_GATE_MARKER=str(marker),
        RC_GATE_DB=f"sqlite:///{(tmp_path / 'mock.db').as_posix()}",
    )
    environment.pop("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", None)
    environment.pop("AGENTFLOW_DEEPSEEK_API_KEY", None)
    if authorization:
        environment["AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION"] = "one-paid-request"
    if key:
        environment["AGENTFLOW_DEEPSEEK_API_KEY"] = synthetic_key
    command = [
        sys.executable,
        "-c",
        LIVE_GATE_PROGRAM,
        "-q",
        "-rs",
        "tests/live/test_deepseek_live.py",
    ]
    if option:
        command.append("--run-live-provider")
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output = completed.stdout + completed.stderr
    if synthetic_key in output:
        pytest.fail("LiveGateCredentialLeak", pytrace=False)
    if completed.returncode:
        pytest.fail("LiveGateSubprocessFailed", pytrace=False)
    if enabled:
        assert "1 passed" in output
        assert marker.read_text() == "mock-only"
    else:
        assert "1 skipped" in output
        reason = (
            "paid provider tests require explicit --run-live-provider"
            if not option
            else "LIVE PROVIDER NOT VERIFIED: explicit paid authorization and key required"
        )
        assert reason in output
        assert not marker.exists()
