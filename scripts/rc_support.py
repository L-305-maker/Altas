"""Isolated RC resources only; never accepts an external database for mutation."""

import os
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(command, *, timeout=30):
    # Tool output can contain connection details. Fail with a safe operation code.
    result = subprocess.run(
        command,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=FLAGS,
    )
    if result.returncode:
        raise RuntimeError(f"RCCommandFailed:{Path(command[0]).stem}")
    return result


class TemporaryPostgres:
    def __init__(self, binaries):
        self.binaries = Path(binaries).resolve()
        self.directory = None
        self.started = False

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agentflow-rc-")
        self.root = Path(self.directory.name).resolve()
        if not self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError("isolated PostgreSQL must stay in OS temporary directory")
        self.data = self.root / "data"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        try:
            run(
                [
                    str(self.binaries / "initdb"),
                    "-D",
                    str(self.data),
                    "-U",
                    "agentflow",
                    "--auth=trust",
                    "--encoding=UTF8",
                    "--locale=C",
                ]
            )
            self.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def start(self):
        run(
            [
                str(self.binaries / "pg_ctl"),
                "-D",
                str(self.data),
                "-l",
                str(self.root / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {self.port} -c unix_socket_directories= -c statement_timeout=5000 -c lock_timeout=2000",
                "-w",
                "start",
            ]
        )
        self.started = True

    def stop(self):
        if self.started or (self.data / "postmaster.pid").exists():
            run(
                [
                    str(self.binaries / "pg_ctl"),
                    "-D",
                    str(self.data),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ]
            )
            self.started = False

    def database(self, name):
        if name not in {"agentflow_rc_source", "agentflow_rc_restored"}:
            raise ValueError("only isolated RC database names are allowed")
        if not self.started:
            raise ValueError("isolated PostgreSQL is not running")
        with psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            user="agentflow",
            dbname="postgres",
            autocommit=True,
            connect_timeout=3,
        ) as db:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return f"postgresql+psycopg://agentflow@127.0.0.1:{self.port}/{name}?connect_timeout=3"

    def __exit__(self, *args):
        try:
            if hasattr(self, "data"):
                self.stop()
        finally:
            if self.directory is not None:
                self.directory.cleanup()


def migrate(url):
    prior = os.environ.get("AGENTFLOW_DATABASE_URL")
    try:
        os.environ["AGENTFLOW_DATABASE_URL"] = url
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "head")
    finally:
        if prior is None:
            os.environ.pop("AGENTFLOW_DATABASE_URL", None)
        else:
            os.environ["AGENTFLOW_DATABASE_URL"] = prior
