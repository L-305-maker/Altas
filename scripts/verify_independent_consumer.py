"""Install a wheel out of tree; exercise only an owned temporary PostgreSQL.

No external database argument, production keys, paid model or business imports.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rc_support import FLAGS, ROOT, TemporaryPostgres


def verify(wheel, binaries):
    wheel = Path(wheel).resolve(strict=True)
    if wheel.suffix != ".whl":
        raise ValueError("ConsumerWheelRequired")
    with tempfile.TemporaryDirectory(prefix="independent-consumer-") as directory:
        root = Path(directory)
        venv = root / "venv"

        def run(command, stage, *, env=None, timeout=180):
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                timeout=timeout,
                creationflags=FLAGS,
            )
            if result.returncode:
                if stage == "ConsumerE2EFailed":
                    try:
                        line = json.loads(result.stdout).get("check_line")
                        if type(line) is int:
                            print(json.dumps({"consumer_check_line": line}))
                    except (ValueError, AttributeError):
                        pass
                raise RuntimeError(stage)
            return result.stdout

        run(
            ["uv", "venv", str(venv), "--python", sys.executable],
            "ConsumerEnvironmentFailed",
            timeout=60,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)],
            "ConsumerInstallFailed",
        )
        fixture = root / "consumer"
        shutil.copytree(
            ROOT / "examples/independent_consumer",
            fixture,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        records = root / "records"
        records.mkdir(mode=0o700)
        with TemporaryPostgres(binaries) as pg:
            url = pg.database("agentflow_rc_source")
            environment = {
                **os.environ,
                "AGENTFLOW_DATABASE_URL": url,
                "AGENTFLOW_PROVIDER": "mock",
                "INDEPENDENT_CONSUMER_DB_URL": url,
                "INDEPENDENT_CONSUMER_RECORDS": str(records),
                "INDEPENDENT_CONSUMER_API_TOKEN": secrets.token_urlsafe(32),
            }
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            # No alembic.ini prepend_sys_path: migrations import the INSTALLED Core.
            run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import sys; from alembic import command; from alembic.config import Config; "
                    "c=Config(); c.set_main_option('script_location', sys.argv[1]); command.upgrade(c, 'head')",
                    str(ROOT / "migrations"),
                ],
                "ConsumerMigrationFailed",
                env=environment,
                timeout=60,
            )
            raw = run(
                [str(python), "-I", str(fixture / "verify.py")],
                "ConsumerE2EFailed",
                env=environment,
                timeout=180,
            )
            result = json.loads(raw)
            if result.get("status") != "PASS":
                raise RuntimeError("IndependentConsumerRejected")
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--bin", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.wheel, args.bin)))
        return 0
    except Exception as error:
        safe = {
            "ConsumerEnvironmentFailed",
            "ConsumerInstallFailed",
            "ConsumerMigrationFailed",
            "ConsumerE2EFailed",
        }
        code = (
            error.args[0]
            if len(error.args) == 1
            and isinstance(error.args[0], str)
            and error.args[0] in safe
            else "IndependentConsumerVerificationFailed"
        )
        print(json.dumps({"status": "FAIL", "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
