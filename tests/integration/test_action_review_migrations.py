import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from agentflow.infrastructure.persistence.models import Base
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_review_upgrade_downgrade_preserves_old_agent_data(
    backend, tmp_path, monkeypatch
):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if backend == "postgres"
        else f"sqlite:///{(tmp_path / 'review-migration.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    with store.engine.begin() as db:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
    monkeypatch.setenv("AGENTFLOW_DATABASE_URL", url)
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0002")
        with store.engine.begin() as db:
            # Existing rows must not acquire a tool binding or action authorization.
            db.execute(
                text(
                    "INSERT INTO workflows(id,name,version,spec) VALUES ('w','old',1,'{}')"
                )
            )
            db.execute(
                text(
                    "INSERT INTO runs(id,workflow_id,status,spec,inputs,created_at) VALUES ('r','w','running','{}','{}',0)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO steps(id,run_id,key,status,attempts,available_at) VALUES ('step','r','work','running',1,0)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO agent_sessions(id,step_row_id,initial_snapshot,snapshot,revision,created_at,updated_at) VALUES ('s','step','{}','{}',0,0,0)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO agent_model_calls(id,session_id,model_call_id,round_index,status,attempts,created_at,updated_at) VALUES ('c','s','m',1,'succeeded',1,0,0)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO agent_messages(id,session_id,sequence,role,payload,created_at) VALUES ('msg','s',0,'user','{}',0)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO agent_actions(session_id,id,model_call_id,call_index,tool_name,arguments_digest,arguments,side_effect,status,idempotency_key,created_at,updated_at) VALUES ('s','a','m',0,'write','digest','{}','unsafe','proposed','stable',0,0)"
                )
            )
        command.upgrade(config, "0003")
        command.upgrade(config, "head")
        command.check(config)
        with store.engine.connect() as db:
            assert db.execute(
                text("SELECT continuation_pending FROM agent_sessions")
            ).scalar_one() in (False, 0)
            assert (
                db.execute(text("SELECT tool_binding FROM agent_actions")).scalar_one()
                is None
            )
            assert (
                db.execute(
                    text("SELECT count(*) FROM agent_action_approvals")
                ).scalar_one()
                == 0
            )
        command.downgrade(config, "0002")
        assert "agent_action_approvals" not in inspect(store.engine).get_table_names()
        assert "continuation_pending" not in {
            c["name"] for c in inspect(store.engine).get_columns("agent_sessions")
        }
        command.upgrade(config, "0003")
        command.upgrade(config, "head")
        command.check(config)
        with store.engine.connect() as db:
            for table in [
                "workflows",
                "runs",
                "steps",
                "agent_sessions",
                "agent_model_calls",
                "agent_messages",
                "agent_actions",
            ]:
                assert (
                    db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1
                )
            assert (
                db.execute(
                    text("SELECT idempotency_key FROM agent_actions")
                ).scalar_one()
                == "stable"
            )
    finally:
        command.downgrade(config, "base")
        store.close()
