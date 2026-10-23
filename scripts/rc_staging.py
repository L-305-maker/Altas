"""Staging-only release evidence and bounded READ ONLY deployment checks.

No migration, remote fault injection, network discovery or credential output.
Host receipts are inventory inputs, not proof of independent physical hosts.
"""

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rc_support import ROOT
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from agentflow import __version__
from agentflow.application.ports.model_request import request_digest
from agentflow.application.ports.model_usage import UsageMappingRegistry
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.infrastructure.persistence.models import Base

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Status = Literal["PASS", "FAIL", "NOT_VERIFIED", "NOT_APPLICABLE"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def required_revision():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


class DeploymentIdentity(Frozen):
    git_commit_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    application_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    runtime_digest: Digest
    admission_digest: Digest
    usage_mapping_digest: Digest
    tool_digest: Digest
    request_contract_versions: tuple[Literal[1, 2], ...] = (1, 2)
    counter_profile_digests: tuple[Digest, ...] = ()


class HostIdentity(Frozen):
    role: Literal["api", "worker"]
    host_digest: Digest
    deployment: DeploymentIdentity


class StagingConfiguration(Frozen):
    environment_id: UUID
    database_oid: int = Field(strict=True, ge=1)
    expected_identity: DeploymentIdentity
    hosts: tuple[HostIdentity, ...] = Field(min_length=3, max_length=4)
    max_workers: int = Field(default=2, strict=True, ge=2, le=3)

    @model_validator(mode="after")
    def inventory(self):
        workers = [h for h in self.hosts if h.role == "worker"]
        if (
            len(workers) != self.max_workers
            or len(self.hosts) != self.max_workers + 1
            or len({h.host_digest for h in workers}) != self.max_workers
            or any(h.deployment != self.expected_identity for h in self.hosts)
        ):
            raise ValueError("StagingInventoryMismatch")
        return self


class Evidence(Frozen):
    source: Literal["readonly_preflight"]
    digest: Digest
    checks: int = Field(strict=True, ge=1, le=10000)
    results: tuple[
        tuple[
            Literal[
                "schema",
                "build",
                "environment_matches",
                "database_matches",
                "tls",
                "safe_role",
                "permissions",
                "timeouts",
            ],
            Literal["PASS", "FAIL"],
        ],
        ...,
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def checksum(self):
        if self.checks != len(self.results) or self.digest != request_digest(
            self.results
        ):
            raise ValueError("EvidenceDigestMismatch")
        return self


class Gate(Frozen):
    status: Status = "NOT_VERIFIED"
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def witnessed(self):
        if self.status == "PASS" and self.evidence is None:
            raise ValueError("ExecutionEvidenceRequired")
        if self.status == "PASS" and any(s != "PASS" for _, s in self.evidence.results):
            raise ValueError("ExecutionEvidenceFailed")
        return self


class RCStagingEvidenceReport(Frozen):
    schema_version: Literal[1] = 1
    git_commit_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    application_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    database_schema_revision: Annotated[str, Field(pattern=r"^\d{4}$")] | None = None
    test_environment_id: UUID
    staging_authorization_state: Literal["readonly", "synthetic_execution"] = "readonly"
    preflight: Gate = Gate()
    host_evidence: Gate = Gate()
    provider_acceptance: Gate = Gate()
    cross_host_recovery: Gate = Gate()
    database_recovery: Gate = Gate()
    fencing_verification: Gate = Gate()
    admission_verification: Gate = Gate()
    summary_recovery: Gate = Gate()
    token_conformance: Gate = Gate()
    started_at: datetime
    completed_at: datetime
    limitations: tuple[
        Literal[
            "InventoryIsNotIndependentHostProof",
            "CrossHostExecutionNotVerified",
            "LiveProviderNotVerified",
            "LiveTokenConformanceNotVerified",
            "IndependentPagesAreNotOneSnapshot",
            "LogicalBackupIsNotHAPITR",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def scope(self):
        # This collector has no external attestation verifier. Imported bools,
        # digests, host inventories or local tests cannot certify these gates.
        for name in (
            "host_evidence",
            "provider_acceptance",
            "cross_host_recovery",
            "database_recovery",
            "fencing_verification",
            "admission_verification",
            "summary_recovery",
            "token_conformance",
        ):
            if getattr(self, name).status == "PASS":
                raise ValueError("IndependentExecutionReviewRequired")
        if self.preflight.status == "PASS":
            evidence = self.preflight.evidence
            if evidence.source != "readonly_preflight" or {
                name for name, _ in evidence.results
            } != {
                "schema",
                "build",
                "environment_matches",
                "database_matches",
                "tls",
                "safe_role",
                "permissions",
                "timeouts",
            }:
                raise ValueError("EvidenceSourceMismatch")
        if (
            self.completed_at < self.started_at
            or self.started_at.utcoffset() is None
            or self.completed_at.utcoffset() is None
        ):
            raise ValueError("InvalidEvidenceTime")
        return self


class DatabaseFacts(Frozen):
    revision: Annotated[str, Field(pattern=r"^\d{4}$")] | None
    environment_matches: bool
    database_matches: bool
    tls: bool
    safe_role: bool
    permissions: bool
    timeouts: bool


def deployment_identity():
    def git(*args):
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise ValueError("BuildIdentityUnavailable")
        return result.stdout.strip()

    if git("status", "--porcelain"):
        raise ValueError("UncommittedStagingBuild")
    paths = sorted((ROOT / "src/agentflow").rglob("*.py"))
    paths += [
        ROOT / "scripts/rc_workload.py",
        ROOT / "scripts/rc_staging.py",
        ROOT / "scripts/rc_host_witness.py",
    ]
    runtime = request_digest(
        [
            (p.relative_to(ROOT).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in paths
        ]
    )
    policies = []
    for name, cls in (
        ("AGENTFLOW_RC_ADMISSION_POLICY", ProviderAdmissionPolicy),
        ("AGENTFLOW_RC_OBSERVED_USAGE_POLICY", ObservedUsagePolicy),
    ):
        value = os.environ.get(name)
        policies.append(
            cls.model_validate_json(value).model_dump(mode="json") if value else None
        )
    return DeploymentIdentity(
        git_commit_sha=git("rev-parse", "HEAD"),
        application_version=__version__,
        runtime_digest=runtime,
        admission_digest=request_digest(policies),
        usage_mapping_digest=request_digest(
            [m.model_dump() for m in UsageMappingRegistry().definitions]
        ),
        tool_digest=hashlib.sha256(
            (ROOT / "scripts/rc_workload.py").read_bytes()
        ).hexdigest(),
    )


def host_identity(role):
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            machine = winreg.QueryValueEx(key, "MachineGuid")[0]
    else:
        machine = Path("/etc/machine-id").read_text("utf-8").strip()
    if not machine:
        raise ValueError("HostIdentityUnavailable")
    return HostIdentity(
        role=role,
        host_digest=hashlib.sha256(machine.encode()).hexdigest(),
        deployment=deployment_identity(),
    )


def require_execution(environment_id, allowed):
    if (
        not allowed
        or os.environ.get("AGENTFLOW_RC_STAGING_AUTHORIZATION")
        != f"synthetic-staging-only:{environment_id}"
    ):
        raise ValueError("StagingAuthorizationRequired")


def validate_url(url, mode):
    parsed = make_url(url)
    production = os.environ.get("AGENTFLOW_PRODUCTION_DATABASE_URL")
    if (
        mode not in {"audit", "worker"}
        or parsed.drivername != "postgresql+psycopg"
        or not (parsed.database or "").startswith("agentflow_rc_")
        or parsed.query.get("sslmode") != "verify-full"
        or not str(parsed.query.get("connect_timeout", "")).isdigit()
        or not 1 <= int(parsed.query["connect_timeout"]) <= 5
        or os.environ.get("AGENTFLOW_ENVIRONMENT", "").lower() != "staging"
        or (
            production
            and make_url(production)._replace(password=None, username=None, query={})
            == parsed._replace(password=None, username=None, query={})
        )
    ):
        raise ValueError("StagingConnectionRejected")


def connect(url, mode):
    validate_url(url, mode)
    return create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})


def read_database_facts(engine, config, *, mode="audit"):
    if engine.dialect.name != "postgresql":
        raise ValueError("StagingPostgresRequired")
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        # Inspect role/server defaults before applying stricter local diagnostic
        # limits: these are the defaults used by the fixture Store's own pool.
        default_timeouts = (
            connection.execute(
                text(
                    "SELECT setting::int FROM pg_settings WHERE name IN ('statement_timeout', 'lock_timeout') ORDER BY name"
                )
            )
            .scalars()
            .all()
        )
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        connection.execute(text("SET LOCAL lock_timeout = '2s'"))
        row = (
            connection.execute(
                text("""
            SELECT d.oid, shobj_description(d.oid, 'pg_database') AS marker,
                NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR r.rolreplication
                     OR pg_has_role(d.datdba, 'MEMBER'))
                AND NOT EXISTS (SELECT 1 FROM pg_roles p WHERE p.rolname IN
                    ('pg_read_server_files', 'pg_write_server_files', 'pg_execute_server_program',
                     'pg_signal_backend', 'pg_read_all_data', 'pg_write_all_data',
                     'pg_checkpoint', 'pg_maintain', 'pg_create_subscription')
                    AND pg_has_role(p.oid, 'MEMBER')) AS safe_role,
                EXISTS (SELECT 1 FROM pg_stat_ssl WHERE pid = pg_backend_pid()
                        AND ssl) AS tls
            FROM pg_database d CROSS JOIN pg_roles r
            WHERE d.datname = current_database() AND r.rolname = current_user
        """)
            )
            .mappings()
            .one()
        )
        revisions = (
            connection.execute(text("SELECT version_num FROM alembic_version LIMIT 2"))
            .scalars()
            .all()
        )
        # Finite application table list; inspect both required DML and excess DDL.
        tables = (*sorted(Base.metadata.tables), "alembic_version")
        privileges_by_table = (
            connection.execute(
                text("""
            WITH requested AS (SELECT unnest(CAST(:tables AS text[])) AS table_name)
            SELECT table_name, has_table_privilege(current_user, c.oid, 'SELECT') AS readable,
                has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS writable,
                has_table_privilege(current_user, c.oid, 'INSERT') AND
                has_table_privilege(current_user, c.oid, 'UPDATE') AS executable,
                has_table_privilege(current_user, c.oid, 'TRUNCATE,REFERENCES,TRIGGER') AS excessive,
                pg_has_role(c.relowner, 'MEMBER') AS owner
            FROM requested LEFT JOIN pg_class c ON c.oid = to_regclass(table_name)
        """),
                {"tables": list(tables)},
            )
            .mappings()
            .all()
        )
        permissions = len(privileges_by_table) == len(tables)
        for privileges in privileges_by_table:
            permissions &= bool(privileges["readable"]) and not privileges["owner"]
            permissions &= (
                bool(privileges["executable"]) and not privileges["excessive"]
                if mode == "worker" and privileges["table_name"] != "alembic_version"
                else not privileges["writable"]
            )
        schema_create = connection.scalar(
            text(
                "SELECT has_schema_privilege(current_user, current_schema(), 'CREATE')"
            )
        )
        return DatabaseFacts(
            revision=revisions[0] if len(revisions) == 1 else None,
            environment_matches=row["marker"]
            == f"agentflow-rc:{config.environment_id}",
            database_matches=row["oid"] == config.database_oid,
            tls=row["tls"],
            safe_role=row["safe_role"] and not schema_create,
            permissions=permissions,
            timeouts=len(default_timeouts) == 2
            and 0 < default_timeouts[0] <= 2000
            and 0 < default_timeouts[1] <= 5000,
        )


def evaluate(config, facts, identity):
    errors = []
    if facts.revision != required_revision():
        errors.append("SchemaMismatch")
    if identity != config.expected_identity:
        errors.append("BuildMismatch")
    for field in (
        "environment_matches",
        "database_matches",
        "tls",
        "safe_role",
        "permissions",
        "timeouts",
    ):
        if not getattr(facts, field):
            errors.append("StagingIsolationRejected")
    return tuple(sorted(set(errors)))


def report(config, *, facts=None, errors=None, started=None, identity=None):
    now = datetime.now(timezone.utc)
    gate = Gate()
    if errors is not None:
        results = (
            ("schema", "PASS" if facts.revision == required_revision() else "FAIL"),
            ("build", "FAIL" if "BuildMismatch" in errors else "PASS"),
            *(
                (name, "PASS" if getattr(facts, name) else "FAIL")
                for name in (
                    "environment_matches",
                    "database_matches",
                    "tls",
                    "safe_role",
                    "permissions",
                    "timeouts",
                )
            ),
        )
        gate = Gate(
            status="FAIL" if errors else "PASS",
            evidence=Evidence(
                source="readonly_preflight",
                digest=request_digest(results),
                checks=len(results),
                results=results,
            ),
        )
    identity = identity or config.expected_identity
    return RCStagingEvidenceReport(
        git_commit_sha=identity.git_commit_sha,
        application_version=identity.application_version,
        database_schema_revision=facts.revision if facts else None,
        test_environment_id=config.environment_id,
        preflight=gate,
        started_at=started or now,
        completed_at=now,
        limitations=(
            "InventoryIsNotIndependentHostProof",
            "CrossHostExecutionNotVerified",
            "LiveProviderNotVerified",
            "LiveTokenConformanceNotVerified",
            "IndependentPagesAreNotOneSnapshot",
            "LogicalBackupIsNotHAPITR",
        ),
    )
