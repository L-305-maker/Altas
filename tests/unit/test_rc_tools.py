import importlib.util
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
