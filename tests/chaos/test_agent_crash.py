"""真实进程在模型预约提交后退出；恢复仍受租约与逻辑调用身份约束。"""

import os
import subprocess
import sys
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from agentflow.application.ports.agent_store import LostLease
from agentflow.domain.agent.status import AgentStatus
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import Store


def test_agent_process_crash_preserves_model_reservation(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'agent-crash.db').as_posix()}")
    store.create_schema()
    store.submit(
        store.register(
            WorkflowSpec(
                name="agent-crash",
                steps=(StepSpec(id="work", handler="agent", retry_delay=0),),
            )
        ),
        {},
    )
    program = """
import os
from agentflow.application.ports.agent_store import AgentTransition
from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.agent.status import AgentStatus
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
store = Store(os.environ['AGENTFLOW_CRASH_TEST_DB'])
agents = SQLAgentStore(store)
task = store.claim(300)
initial = AgentSnapshot(id='crashed-agent', budget=AgentBudget(
    max_model_rounds=2, max_model_attempts=3, max_tool_calls=3, max_resource_units=100))
agents.open(task, initial)
candidate = AgentSnapshot.model_validate({**initial.model_dump(), 'revision': 1,
    'status': AgentStatus.MODEL_INFLIGHT, 'model_call_id': 'reserved-before-crash',
    'budget': initial.budget.consume(model_rounds=1, model_attempts=1)})
agents.commit_transition(task, AgentTransition(snapshot=candidate))
os._exit(13)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            env=dict(os.environ, AGENTFLOW_CRASH_TEST_DB=str(store.engine.url)),
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 13
        with store.transaction() as db:
            old_token = db.scalar(select(StepRow.token))
            db.execute(update(StepRow).values(lease_until=0))
        task = store.claim(300)
        recovered = SQLAgentStore(store).load(task)
        assert recovered.snapshot.status == AgentStatus.MODEL_INFLIGHT
        assert recovered.snapshot.budget.model_attempts == 1
        assert recovered.model_calls[0].model_call_id == "reserved-before-crash"
        assert recovered.model_calls[0].status == "unknown"
        with pytest.raises(LostLease):
            SQLAgentStore(store).load(replace(task, token=old_token))
    finally:
        store.close()
