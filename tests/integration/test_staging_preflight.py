from pathlib import Path

import pytest
from sqlalchemy import event, text
from test_durable_agent_runner import store as store


def staging(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import rc_staging

    return rc_staging


def test_preflight_does_not_accept_sqlite_or_modify_it(store, monkeypatch):
    module = staging(monkeypatch)
    if store.engine.dialect.name == "sqlite":
        with pytest.raises(ValueError, match="StagingPostgresRequired"):
            module.read_database_facts(store.engine, None)
        return
    from uuid import uuid4

    environment = uuid4()
    with store.engine.begin() as db:
        existed = db.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
        db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version(version_num varchar(32) PRIMARY KEY)"
            )
        )
        previous = (
            db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        )
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(text("INSERT INTO alembic_version VALUES ('0005')"))
        oid = db.scalar(
            text("SELECT oid FROM pg_database WHERE datname = current_database()")
        )
    # No fabricated production-safe facts: isolated test server has no TLS,
    # administrative test role, and no administrator environment marker.
    from types import SimpleNamespace

    config = SimpleNamespace(environment_id=environment, database_oid=oid)
    statements = []

    def observe(conn, cursor, statement, *args):
        statements.append(statement.upper())

    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        facts = module.read_database_facts(store.engine, config)
        assert facts.revision == "0005"
        assert facts.database_matches
        assert not facts.environment_matches and not facts.tls and not facts.safe_role
        assert not facts.permissions
        assert any("READ ONLY" in s for s in statements)
        assert not any(
            s.startswith(("UPDATE", "INSERT", "DELETE", "CREATE", "ALTER", "DROP"))
            for s in statements
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)
        with store.engine.begin() as db:
            db.execute(text("DELETE FROM alembic_version"))
            for revision in previous:
                db.execute(
                    text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision}
                )
            if not existed:
                db.execute(text("DROP TABLE alembic_version"))


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_preflight_observes_real_least_privilege_roles_and_admin_marker(
    store, monkeypatch
):
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    module = staging(monkeypatch)
    environment = uuid4()
    role = "rc_reader_" + uuid4().hex
    with store.engine.begin() as db:
        existed = db.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
        db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version(version_num varchar(32) PRIMARY KEY)"
            )
        )
        database = db.scalar(text("SELECT current_database()"))
        oid = db.scalar(
            text("SELECT oid FROM pg_database WHERE datname = current_database()")
        )
        marker = db.scalar(
            text("SELECT shobj_description(:oid, 'pg_database')"), {"oid": oid}
        )
        db.execute(text(f'CREATE ROLE "{role}" LOGIN'))
        db.execute(text(f'ALTER ROLE "{role}" SET statement_timeout = 5000'))
        db.execute(text(f'ALTER ROLE "{role}" SET lock_timeout = 2000'))
        db.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        db.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
        # All identifiers/marker here belong to the isolated test cluster.
        db.execute(
            text(f"COMMENT ON DATABASE \"{database}\" IS 'agentflow-rc:{environment}'")
        )
    engine = create_engine(store.engine.url.set(username=role), poolclass=NullPool)
    try:
        config = SimpleNamespace(environment_id=environment, database_oid=oid)
        facts = module.read_database_facts(engine, config)
        assert facts.permissions and facts.safe_role and facts.timeouts
        assert facts.environment_matches and facts.database_matches
        assert not facts.tls  # Local trust test cluster cannot certify staging TLS.
        with store.engine.begin() as db:
            for table in module.Base.metadata.tables:
                db.execute(text(f'GRANT INSERT, UPDATE ON "{table}" TO "{role}"'))
        assert module.read_database_facts(engine, config, mode="worker").permissions
        assert not module.read_database_facts(engine, config, mode="audit").permissions
        with store.engine.begin() as db:
            db.execute(text(f'GRANT pg_write_all_data TO "{role}"'))
        assert not module.read_database_facts(engine, config, mode="worker").safe_role
    finally:
        engine.dispose()
        with store.engine.begin() as db:
            original = (
                "NULL" if marker is None else "'" + marker.replace("'", "''") + "'"
            )
            db.execute(text(f'COMMENT ON DATABASE "{database}" IS {original}'))
            db.execute(text(f'DROP OWNED BY "{role}"'))
            db.execute(text(f'DROP ROLE "{role}"'))
            if not existed:
                db.execute(text("DROP TABLE alembic_version"))


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["postgres"], indirect=True)
async def test_staging_worker_slots_use_independent_pg_sessions_and_cancel_cleanly(
    store, monkeypatch
):
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4

    staging(monkeypatch)
    import rc_workload
    import verify_rc_staging as module

    environment = str(uuid4())
    config = SimpleNamespace(
        environment_id=environment,
        max_workers=2,
        hosts=[SimpleNamespace(role="worker"), SimpleNamespace(role="worker")],
    )
    monkeypatch.setattr(module, "configuration", lambda *a: config)
    monkeypatch.setattr(module, "host_identity", lambda *a: config.hosts[0])
    monkeypatch.setattr(module, "preflight", lambda *a, **k: (None, ()))
    monkeypatch.setattr(module, "connect", lambda *a: store.engine)
    monkeypatch.setenv("AGENTFLOW_RC_DATABASE_URL", "not-used")
    monkeypatch.setenv("AGENTFLOW_RC_RECORDS", "not-used")
    monkeypatch.setenv(
        "AGENTFLOW_RC_STAGING_AUTHORIZATION", f"synthetic-staging-only:{environment}"
    )
    started, stopped = asyncio.Event(), asyncio.Event()

    async def serve(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(rc_workload, "serve", serve)
    kwargs = dict(config_path="not-used", environment=environment, allowed=True, slot=0)
    task = asyncio.create_task(module.worker(30, **kwargs))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ValueError, match="StagingWorkerSlotOccupied"):
            await asyncio.wait_for(module.worker(30, **kwargs), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 7)
    assert stopped.is_set()
    # Session-scoped slot is released; no owned coroutine left running.
    namespace = int(module.request_digest(environment)[:7], 16)
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        assert db.scalar(text("SELECT pg_try_advisory_lock(:n, 0)"), {"n": namespace})
        assert db.scalar(text("SELECT pg_advisory_unlock(:n, 0)"), {"n": namespace})
    assert not [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
