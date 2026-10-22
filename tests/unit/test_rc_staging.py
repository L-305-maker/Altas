import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_rc_tools import script

ENVIRONMENT = UUID("11111111-2222-4333-8444-555555555555")


def configuration(module):
    identity = module.DeploymentIdentity(
        git_commit_sha="a" * 40,
        application_version="1.0.0",
        runtime_digest="b" * 64,
        admission_digest="c" * 64,
        usage_mapping_digest="d" * 64,
        tool_digest="e" * 64,
    )
    return module.StagingConfiguration(
        environment_id=ENVIRONMENT,
        database_oid=12345,
        expected_identity=identity,
        hosts=tuple(
            module.HostIdentity(role=role, host_digest=digit * 64, deployment=identity)
            for role, digit in (("api", "1"), ("worker", "2"), ("worker", "3"))
        ),
    )


def facts(module, **changes):
    return module.DatabaseFacts(
        revision=module.required_revision(),
        environment_matches=True,
        database_matches=True,
        tls=True,
        safe_role=True,
        permissions=True,
        timeouts=True,
        **changes,
    )


def test_staging_runbook_uses_actual_schema_head(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    text = (module.ROOT / "docs/production-recovery-runbook.md").read_text("utf-8")
    section = text.split("## 可选两宿主 staging", 1)[1].split("## ", 1)[0]
    assert f"Schema head：`{module.required_revision()}`" in section
    assert "初始化到 0005" not in section


def test_evidence_cannot_promote_missing_or_local_execution(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    with pytest.raises(ValidationError):
        module.Gate(status="PASS")
    evidence = module.Evidence(
        source="readonly_preflight",
        digest=module.request_digest((("build", "PASS"),)),
        checks=1,
        results=(("build", "PASS"),),
    )
    report = module.report(configuration(module))
    data = report.model_dump()
    data["cross_host_recovery"] = module.Gate(status="PASS", evidence=evidence)
    with pytest.raises(ValidationError):
        module.RCStagingEvidenceReport.model_validate(data)
    data["cross_host_recovery"] = module.Gate()
    data["provider_acceptance"] = module.Gate(status="PASS", evidence=evidence)
    with pytest.raises(ValidationError):
        module.RCStagingEvidenceReport.model_validate(data)
    with pytest.raises(ValidationError):
        module.Gate(status="PASS", passed=True)
    assert report.cross_host_recovery.status == "NOT_VERIFIED"
    assert report.provider_acceptance.status == "NOT_VERIFIED"
    assert report.token_conformance.status == "NOT_VERIFIED"
    with pytest.raises(ValidationError):
        report.application_version = "changed"


def test_preflight_requires_exact_build_and_distinct_host_inventory(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    config = configuration(module)
    assert module.evaluate(config, facts(module), config.expected_identity) == ()
    identity = config.expected_identity.model_dump()
    identity["git_commit_sha"] = "f" * 40
    assert "BuildMismatch" in module.evaluate(
        config, facts(module), module.DeploymentIdentity(**identity)
    )
    data = config.model_dump()
    data["hosts"] = [config.hosts[0], config.hosts[1], config.hosts[1]]
    with pytest.raises(ValidationError):
        module.StagingConfiguration.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "environment_matches",
        "database_matches",
        "tls",
        "safe_role",
        "permissions",
        "timeouts",
    ],
)
def test_name_prefix_cannot_replace_observed_isolation(monkeypatch, field):
    module = script(monkeypatch, "rc_staging")
    observed = facts(module).model_dump()
    observed[field] = False
    config = configuration(module)
    assert module.evaluate(
        config, module.DatabaseFacts(**observed), config.expected_identity
    )
    observed["revision"] = "0005"
    assert "SchemaMismatch" in module.evaluate(
        config, module.DatabaseFacts(**observed), config.expected_identity
    )


def test_refusal_happens_before_connect_or_faults(monkeypatch, capsys):
    module = script(monkeypatch, "verify_rc_staging")
    secret = "synthetic-secret-never-print"
    monkeypatch.setenv(
        "AGENTFLOW_RC_DATABASE_URL",
        f"postgresql://u:{secret}@invalid/agentflow_rc_forged",
    )
    monkeypatch.setattr(sys, "argv", ["verify_rc_staging.py", "seed"])
    monkeypatch.setattr(
        module, "connect", lambda *a, **k: pytest.fail("ExternalConnection")
    )
    assert module.main() == 2
    output = capsys.readouterr()
    assert "StagingAuthorizationRequired" in output.out
    assert secret not in output.out + output.err


def test_production_url_and_unverified_tls_are_rejected_without_connect(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    with pytest.raises(ValueError, match="StagingConnectionRejected"):
        module.validate_url("sqlite:///agentflow_rc_fake", "audit")
    with pytest.raises(ValueError, match="StagingConnectionRejected"):
        module.validate_url("postgresql://host/agentflow_rc_fake", "audit")
    url = "postgresql+psycopg://u:p@host/agentflow_rc_stage?sslmode=verify-full&connect_timeout=3"
    monkeypatch.setenv("AGENTFLOW_ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="StagingConnectionRejected"):
        module.validate_url(url, "audit")
    monkeypatch.setenv("AGENTFLOW_ENVIRONMENT", "staging")
    monkeypatch.setenv(
        "AGENTFLOW_PRODUCTION_DATABASE_URL", url.replace("u:p", "different:credentials")
    )
    with pytest.raises(ValueError, match="StagingConnectionRejected"):
        module.validate_url(url, "audit")


def test_execution_authorization_is_environment_bound(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    config = configuration(module)
    for flag, value in (
        (False, ""),
        (True, ""),
        (True, "synthetic-staging-only:wrong"),
    ):
        monkeypatch.setenv("AGENTFLOW_RC_STAGING_AUTHORIZATION", value)
        with pytest.raises(ValueError, match="StagingAuthorizationRequired"):
            module.require_execution(config.environment_id, flag)
    monkeypatch.setenv(
        "AGENTFLOW_RC_STAGING_AUTHORIZATION", f"synthetic-staging-only:{ENVIRONMENT}"
    )
    module.require_execution(config.environment_id, True)


def test_reports_have_no_free_form_content_or_credentials(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    report = module.report(configuration(module))
    assert "NOT_VERIFIED" in report.model_dump_json()
    for key in ("prompt", "tool_result", "database_url", "token", "request_id"):
        data = report.model_dump()
        data[key] = "PRIVATE"
        with pytest.raises(ValidationError):
            module.RCStagingEvidenceReport.model_validate(data)
    assert "PRIVATE" not in json.dumps(report.model_dump(mode="json"))


def test_worker_staging_refuses_missing_authorization_before_serve(monkeypatch, capsys):
    module = script(monkeypatch, "verify_durable_soak")
    monkeypatch.setattr(sys, "argv", ["verify_durable_soak.py", "--worker"])
    workload = script(monkeypatch, "rc_workload")
    monkeypatch.setattr(workload, "serve", lambda *a, **k: pytest.fail("WorkerStarted"))
    assert module.main() == 2
    assert "StagingAuthorizationRequired" in capsys.readouterr().out


def test_no_new_migration_or_stale_head(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    assert module.required_revision() == "0008"
    assert len(list((Path(module.ROOT) / "migrations/versions").glob("*.py"))) == 8


def test_evidence_digest_and_scope_cannot_be_relabelled(monkeypatch):
    module = script(monkeypatch, "rc_staging")
    with pytest.raises(ValidationError):
        module.Evidence(
            source="readonly_preflight",
            digest="a" * 64,
            checks=1,
            results=(("build", "PASS"),),
        )
    result = module.report(configuration(module), facts=facts(module), errors=())
    assert result.preflight.status == "PASS"
    assert len(result.preflight.evidence.results) == 8
    assert result.cross_host_recovery.status == "NOT_VERIFIED"
    data = result.model_dump()
    data["preflight"]["evidence"]["results"] = (("build", "PASS"),)
    data["preflight"]["evidence"]["checks"] = 1
    data["preflight"]["evidence"]["digest"] = module.request_digest(
        (("build", "PASS"),)
    )
    with pytest.raises(ValidationError):
        module.RCStagingEvidenceReport.model_validate(data)


@pytest.mark.asyncio
async def test_mismatched_schema_stops_authorized_worker_before_start(monkeypatch):
    module = script(monkeypatch, "verify_rc_staging")
    core = script(monkeypatch, "rc_staging")
    config = configuration(core)
    monkeypatch.setattr(module, "configuration", lambda *a: config)
    monkeypatch.setattr(module, "host_identity", lambda *a: config.hosts[1])
    monkeypatch.setattr(
        module, "preflight", lambda *a, **k: (None, ("SchemaMismatch",))
    )
    monkeypatch.setattr(module, "connect", lambda *a, **k: pytest.fail("WorkerStarted"))
    monkeypatch.setenv(
        "AGENTFLOW_RC_STAGING_AUTHORIZATION", f"synthetic-staging-only:{ENVIRONMENT}"
    )
    with pytest.raises(ValueError, match="StagingPreflightRejected"):
        await module.worker(
            1, config_path="unused", environment=str(ENVIRONMENT), allowed=True, slot=0
        )


@pytest.mark.asyncio
async def test_lost_slot_connection_cancels_only_owned_fixture_coroutine(monkeypatch):
    import asyncio

    module = script(monkeypatch, "verify_rc_staging")
    core = script(monkeypatch, "rc_staging")
    workload = script(monkeypatch, "rc_workload")
    config = configuration(core)
    stopped = asyncio.Event()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execution_options(self, **kwargs):
            return self

        def scalar(self, *args):
            return True

        def execute(self, statement):
            if str(statement) == "SELECT 1":
                raise RuntimeError("SyntheticConnectionLost")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            raise RuntimeError("SyntheticCloseFailure")

    async def serve(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(module, "configuration", lambda *a: config)
    monkeypatch.setattr(module, "host_identity", lambda *a: config.hosts[1])
    monkeypatch.setattr(module, "preflight", lambda *a, **k: (None, ()))
    monkeypatch.setattr(module, "connect", lambda *a, **k: Engine())
    monkeypatch.setattr(workload, "serve", serve)
    monkeypatch.setenv("AGENTFLOW_RC_DATABASE_URL", "unused")
    monkeypatch.setenv("AGENTFLOW_RC_RECORDS", "unused")
    monkeypatch.setenv(
        "AGENTFLOW_RC_STAGING_AUTHORIZATION", f"synthetic-staging-only:{ENVIRONMENT}"
    )
    with pytest.raises(RuntimeError, match="SyntheticConnectionLost"):
        await asyncio.wait_for(
            module.worker(
                1,
                config_path="unused",
                environment=str(ENVIRONMENT),
                allowed=True,
                slot=0,
            ),
            2,
        )
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_operator_fixture_plan_lifecycle_uses_real_review_and_unknown(
    monkeypatch, tmp_path, capsys
):
    import asyncio
    from types import SimpleNamespace

    module = script(monkeypatch, "verify_rc_staging")
    core = script(monkeypatch, "rc_staging")
    workload = script(monkeypatch, "rc_workload")
    config = configuration(core)
    url = f"sqlite:///{(tmp_path / 'fixtures.db').as_posix()}"
    monkeypatch.setenv("AGENTFLOW_RC_DATABASE_URL", url)
    monkeypatch.setenv("AGENTFLOW_RC_RECORDS", str(tmp_path / "records"))
    for name in ("AGENTFLOW_RC_ADMISSION_POLICY", "AGENTFLOW_RC_OBSERVED_USAGE_POLICY"):
        monkeypatch.delenv(name, raising=False)
    own = workload.Workload(url, tmp_path / "coordinator")
    own.store.create_schema()
    args = SimpleNamespace(
        command="seed",
        scenario="unknown",
        plan=str(tmp_path / "new-private" / "plan.json"),
        runs=1,
        seconds=10,
    )
    try:
        await asyncio.to_thread(module.synthetic, config, args)
        run = json.loads(Path(args.plan).read_text("utf-8"))["runs"][0]["id"]
        await workload.serve(url, tmp_path / "a", 0.5)
        args.command = "approve"
        original = Path(args.plan).read_text("utf-8")
        altered = json.loads(original)
        altered["batch_id"] = "00000000-0000-4000-8000-000000000000"
        Path(args.plan).write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(ValueError, match="StagingPlanRejected"):
            await asyncio.to_thread(module.synthetic, config, args)
        Path(args.plan).write_text(original, encoding="utf-8")
        await asyncio.to_thread(module.synthetic, config, args)
        await workload.serve(url, tmp_path / "b", 0.5)
        assert own.status(run, "waiting_reconciliation")
        before = own.state(run).agent
        await workload.serve(url, tmp_path / "restarted", 0.5)
        assert own.state(run).agent == before
        args.command = "reconcile"
        await asyncio.to_thread(module.synthetic, config, args)
        await workload.serve(url, tmp_path / "c", 0.5)
        assert own.status(run, "succeeded")
        assert len(own.store.artifacts(run)) == 3
        assert "value" not in capsys.readouterr().out
    finally:
        own.close()
