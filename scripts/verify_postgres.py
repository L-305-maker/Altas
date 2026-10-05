"""启动专用临时 PostgreSQL，运行真实数据库测试后关闭；不触碰已有服务。

用法：python scripts/verify_postgres.py --bin D:/PSQL/bin
临时实例仅监听 127.0.0.1，使用随机端口；trust 仅用于此短生命周期测试实例。
"""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True)
    args = parser.parse_args()
    binaries = Path(args.bin).resolve()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(command):
        subprocess.run(
            command,
            check=True,
            creationflags=flags,
            timeout=30,
        )

    with tempfile.TemporaryDirectory(prefix="agentflow-pg-") as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError(
                "temporary database must remain in the OS temp directory"
            )
        data = root / "data"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        run(
            [
                str(binaries / "initdb"),
                "-D",
                str(data),
                "-U",
                "agentflow",
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        ctl = str(binaries / "pg_ctl")
        log = root / "server.log"
        started = False
        try:
            run(
                [
                    ctl,
                    "-D",
                    str(data),
                    "-l",
                    str(log),
                    "-o",
                    # 所有测试均通过 TCP 连接。禁用 Unix socket，避免 Linux
                    # 发行版的默认 socket 目录属于 postgres 用户而 CI 无权写入。
                    f"-h 127.0.0.1 -p {port} -c unix_socket_directories=",
                    "-w",
                    "start",
                ]
            )
            started = True
            environment = dict(
                os.environ,
                AGENTFLOW_TEST_POSTGRES_URL=f"postgresql+psycopg://agentflow@127.0.0.1:{port}/postgres",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/integration/test_postgres.py",
                    "-q",
                ],
                env=environment,
                creationflags=flags,
                capture_output=True,
                text=True,
                check=False,
            )
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # pg_ctl 的输出只有启动摘要，真正的服务端错误写在此日志中。
            # 在 TemporaryDirectory 删除它之前输出，便于排查 CI 环境差异。
            if log.exists():
                print(
                    log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr
                )
            raise
        finally:
            # 启动失败时也可能已有服务进程（例如等待超时），检查 PID 文件
            # 后再清理；清理失败不能遮盖最初的启动错误。
            if started:
                run([ctl, "-D", str(data), "-m", "immediate", "-w", "stop"])
            elif (data / "postmaster.pid").exists():
                try:
                    subprocess.run(
                        [ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                        check=False,
                        creationflags=flags,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    print(f"PostgreSQL cleanup failed: {error}", file=sys.stderr)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
