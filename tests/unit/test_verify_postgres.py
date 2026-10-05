"""验证 CI 临时数据库的故障诊断与生命周期，无需安装 PostgreSQL。"""

import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def verifier(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_postgres.py"
    spec = importlib.util.spec_from_file_location("verify_postgres", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "argv", [str(path), "--bin", "unused"])
    return module


@pytest.mark.parametrize("partial_start", [False, True])
def test_start_failure_keeps_server_diagnostics(
    verifier, monkeypatch, capsys, partial_start
):
    calls = []
    failure = subprocess.CalledProcessError(1, "pg_ctl start")

    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == "start":
            assert "unix_socket_directories=" in command[command.index("-o") + 1]
            Path(command[command.index("-l") + 1]).write_text("startup diagnostic")
            if partial_start:
                data = Path(command[command.index("-D") + 1])
                data.mkdir()
                (data / "postmaster.pid").touch()
            raise failure
        if command[-1] == "stop":
            # 清理本身失败时仍应保留原始启动异常。
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verifier.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        verifier.main()
    assert caught.value is failure
    assert "startup diagnostic" in capsys.readouterr().err
    assert sum(command[-1] == "stop" for command in calls) == int(partial_start)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_test_exit_code_is_preserved_and_server_stopped(
    verifier, monkeypatch, exit_code
):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "pytest" in command:
            return subprocess.CompletedProcess(command, exit_code, "test output", "")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verifier.subprocess, "run", run)
    assert verifier.main() == exit_code
    assert calls[-1][-1] == "stop"
