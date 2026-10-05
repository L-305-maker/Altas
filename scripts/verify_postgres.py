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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        try:
            run(
                [
                    ctl,
                    "-D",
                    str(data),
                    "-l",
                    str(root / "server.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {port}",
                    "-w",
                    "start",
                ]
            )
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
        finally:
            run([ctl, "-D", str(data), "-m", "immediate", "-w", "stop"])
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
