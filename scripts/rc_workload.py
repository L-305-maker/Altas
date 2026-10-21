"""Deterministic, nonsensitive RC fixtures shared by soak and restore drills.

Only the supervisor mutates its own temporary databases. Worker mode may also be
launched explicitly on staging hosts; it never creates/drops schema or data.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
from rc_support import FLAGS, ROOT
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import LostLease
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelRateLimitedError,
    ModelResponse,
    ModelRetryableError,
)
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import RunRow
from agentflow.infrastructure.persistence.store import Store


def task_fence_record(task: Task) -> dict:
    """Only the real lease identity; no prompt, inputs, spec or dependencies."""
    return {
        "run_id": task.run_id,
        "step_id": task.step_id,
        "token": task.token,
        "attempt": task.attempt,
    }


def private_records(root):
    # mkdir(0o700) installs a private Windows ACL starting with Python 3.12.4.
    if os.name == "nt" and sys.version_info < (3, 12, 4):
        raise RuntimeError("RC private records require Windows Python 3.12.4+")
    root = Path(root)
    root.mkdir(mode=0o700, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="worker-", dir=root))


def record(root, kind, **data):
    path = Path(root) / f"worker-{os.getpid()}.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "kind": kind,
                    "pid": os.getpid(),
                    "slot": os.environ.get("AGENTFLOW_RC_SLOT"),
                    **data,
                }
            )
            + "\n"
        )


async def serve(url, root, seconds):
    root = private_records(root)
    configured = os.environ.get("AGENTFLOW_RC_ADMISSION_POLICY")
    policy = (
        ProviderAdmissionPolicy.model_validate_json(configured) if configured else None
    )
    configured_usage = os.environ.get("AGENTFLOW_RC_OBSERVED_USAGE_POLICY")
    usage_policy = (
        ObservedUsagePolicy.model_validate_json(configured_usage)
        if configured_usage
        else None
    )
    store = Store(url)  # Independent engine/pool in every OS process.
    agents, handlers = (
        SQLAgentStore(
            store, admission_policy=policy, observed_usage_policy=usage_policy
        ),
        HandlerRegistry(),
    )

    async def plain(task):
        record(root, "plain", run=task.run_id, step=task.step_id)
        return {"ok": True}

    async def agent(task):
        record(root, "claim", task=task_fence_record(task))
        scenario = task.inputs["scenario"]
        tools = ToolRuntime()

        async def echo(args):
            return {"value": 1}

        async def durable(args, invocation):
            record(
                root,
                "dispatch",
                action=invocation.action_id,
                key=invocation.idempotency_key,
            )
            if scenario == "crash":
                # Parent kills this process after seeing the actual dispatch.
                await asyncio.Event().wait()
            if scenario in {"unknown", "reconciled"}:
                raise TimeoutError("SyntheticUnknown")
            return {"value": 1}

        tools.register(
            Tool(
                "echo",
                "RC fixture",
                {"type": "object"},
                echo,
                requires_approval=scenario
                in {"review", "unknown", "reconciled", "crash"},
                side_effect=ToolSideEffect.READ_ONLY,
                invoke_durable=durable,
            )
        )

        class Provider(MockProvider):
            async def respond(self, messages, tools=None):
                state = await asyncio.to_thread(agents.load, task)
                if scenario == "model_unknown":
                    raise ModelOutcomeUnknown()
                if scenario == "model_rate_limited":
                    raise ModelRateLimitedError(retry_after_seconds=5)
                if scenario == "retry" and state.snapshot.budget.model_attempts == 1:
                    return await MockProvider([ModelRetryableError()]).respond(
                        messages, tools
                    )
                if scenario not in {"final", "retry", "cancel"} and not any(
                    m["role"] == "tool" for m in messages
                ):
                    response = {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "provider-local-id",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    }
                else:
                    response = {"role": "assistant", "content": "RC complete"}
                return ModelResponse(
                    assistant_message=response,
                    finish_reason="tool_calls"
                    if response.get("tool_calls")
                    else "stop",
                    usage={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10},
                    model="rc-mock",
                )

        return await DurableAgentRunner(
            agents, Provider(), tools, admission=agents if policy else None
        ).run(task, "Return a fixed harmless result.", allowed={"echo"})

    handlers.register("rc-agent", agent)
    handlers.register("rc-plain", plain)
    worker = Worker(store, handlers, concurrency=1, lease_seconds=3, poll_seconds=0.05)
    try:
        # Bounded loop; a database outage conservatively stops this process.
        async with asyncio.timeout(seconds):
            while True:
                if not await worker.tick():
                    await asyncio.sleep(0.05)
    except TimeoutError:
        pass
    except SQLAlchemyError:
        record(root, "database_stopped")
    finally:
        store.close()


class Workload:
    def __init__(self, url, root, *, admission_policy=None, observed_usage_policy=None):
        self.url, self.root = url, Path(root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.store = Store(url)
        self.admission_policy = admission_policy
        self.observed_usage_policy = observed_usage_policy
        self.agents = SQLAgentStore(
            self.store,
            admission_policy=admission_policy,
            observed_usage_policy=observed_usage_policy,
        )
        # Reuse the release invariant checker without a second implementation.
        sys.path.insert(0, str(ROOT / "tests"))
        from runtime_assertions import RuntimeInvariants

        self.invariants = RuntimeInvariants()
        self.children, self.runs = [], []
        self.scenarios = {}
        self.deadline = time.monotonic() + 600

    def submit(self, scenario, *, retry_seconds=4):
        workflow = self.store.register(
            WorkflowSpec(
                name=f"rc-{scenario}-{retry_seconds}",
                steps=(
                    StepSpec(
                        id="agent",
                        handler="rc-agent",
                        artifact=True,
                        max_attempts=3,
                        timeout=20,
                        retry_delay=0,
                        parameters={
                            "agent": {
                                "context_projection": {
                                    "mode": "evidence_anchored_recent_rounds"
                                },
                                "evidence_checkpoint": {"mode": "anchored_extracts"},
                                "retry_base_seconds": retry_seconds,
                                "retry_max_seconds": retry_seconds,
                                **(
                                    {"max_model_attempts": 1}
                                    if scenario == "model_rate_limited"
                                    else {}
                                ),
                            }
                        },
                    ),
                    StepSpec(id="peer", handler="rc-plain", artifact=True),
                    StepSpec(
                        id="after",
                        handler="rc-plain",
                        depends_on=("agent", "peer"),
                        artifact=True,
                    ),
                ),
            )
        )
        run = self.store.submit(workflow, {"scenario": scenario})
        self.runs.append(run)
        self.scenarios[run] = scenario
        return run

    def start(self, count=3, seconds=60):
        for _ in range(count):
            slot = uuid4().hex
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/verify_durable_soak.py"),
                    "--worker",
                    "--seconds",
                    str(min(seconds, 3600)),
                ],
                env=dict(
                    os.environ,
                    AGENTFLOW_RC_DATABASE_URL=self.url,
                    AGENTFLOW_RC_RECORDS=str(self.root),
                    AGENTFLOW_RC_SLOT=slot,
                    AGENTFLOW_RC_ADMISSION_POLICY=(
                        self.admission_policy.model_dump_json()
                        if self.admission_policy
                        else ""
                    ),
                    AGENTFLOW_RC_OBSERVED_USAGE_POLICY=(
                        self.observed_usage_policy.model_dump_json()
                        if self.observed_usage_policy
                        else ""
                    ),
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=FLAGS,
            )
            child.rc_slot = slot
            self.children.append(child)

    def events(self):
        result = []
        for path in self.root.glob("worker-*/worker-*.jsonl"):
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    if (
                        index == len(lines) - 1
                        and not line.endswith("\n")
                        and any(p.poll() is None for p in self.children)
                    ):
                        continue  # A live writer may be in its final append.
                    raise AssertionError("InvalidRCWorkerRecord") from None
        return result

    def wait(self, condition, seconds=20):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("RCProgressTimeout")

    def state(self, run):
        return self.invariants(self.store, run).get("agent")

    def status(self, run, status):
        state = self.state(run)
        return state if state and state.agent.snapshot.status == status else None

    def approve(self, run):
        state = self.wait(lambda: self.status(run, "waiting_approval"))
        approval = state.agent.approvals[-1]
        self.agents.decide_action(
            approval.approval_id,
            ApprovalDecision(
                expected_revision=state.agent.snapshot.revision,
                decision="approved",
                actor="rc-operator",
            ),
        )
        return state

    def reconcile(self, run):
        state = self.wait(lambda: self.status(run, "waiting_reconciliation"))
        action = next(
            a.action for a in state.agent.actions if a.action.status == "unknown"
        )
        self.agents.reconcile_action(
            state.agent.snapshot.id,
            action.id,
            ReconciliationDecision(
                request_id=f"rc-{action.id}",
                expected_revision=state.agent.snapshot.revision,
                confirmed_status="succeeded",
                result={"value": 1},
                evidence_reference="synthetic-rc-evidence",
                actor="rc-operator",
            ),
        )
        return state

    def check(self):
        for run in self.runs:
            checkpoint = self.invariants(self.store, run).get("agent")
            if checkpoint and checkpoint.agent.snapshot.status == "succeeded":
                scenario = self.scenarios[run]
                expected = (
                    (1, 2, 0)
                    if scenario == "retry"
                    else (1, 1, 0)
                    if scenario in {"final", "cancel"}
                    else (2, 2, 1)
                )
                budget = checkpoint.agent.snapshot.budget
                assert (
                    budget.model_rounds,
                    budget.model_attempts,
                    budget.tool_calls,
                ) == expected, "UnexpectedReservationCount"
        events = self.events()
        actions = Counter(e["action"] for e in events if e["kind"] == "dispatch")
        assert all(count == 1 for count in actions.values()), "DuplicateActionDispatch"
        claims = Counter(
            (e["task"]["run_id"], e["task"]["step_id"], e["task"]["token"])
            for e in events
            if e["kind"] == "claim"
        )
        assert all(count == 1 for count in claims.values()), "DuplicateClaim"
        return {
            "runs": len(self.runs),
            "dispatches": len(actions),
            "claims": sum(claims.values()),
        }

    def stop(self):
        for child in self.children:
            if child.poll() is None:
                self.kill(child)
        for child in self.children:
            child.wait(timeout=10)
        self.children.clear()

    @staticmethod
    def kill(child):
        if os.name == "nt":
            # A Windows venv launcher has a Python child; kill this owned tree.
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=FLAGS,
            )
        else:
            child.kill()
        child.wait(timeout=10)

    def close(self):
        self.stop()
        self.store.close()

    def fenced_claims(self):
        for entry in self.events():
            if entry["kind"] == "claim":
                identity = entry["task"]
                # Recover the immutable spec in memory from the test database.
                # Payloads are unnecessary for lease checks and stay omitted.
                with self.store.transaction() as db:
                    definition = db.scalar(
                        select(RunRow.spec).where(RunRow.id == identity["run_id"])
                    )
                    if definition is None:
                        raise AssertionError("MissingRCFixtureRun")
                    spec = next(
                        s
                        for s in WorkflowSpec.model_validate(definition).steps
                        if s.id == identity["step_id"]
                    )
                task = Task(
                    identity["run_id"],
                    identity["step_id"],
                    identity["token"],
                    identity["attempt"],
                    spec,
                    {},
                    {},
                )
                if self.state(task.run_id) is None:
                    # A process may die after claim and before AgentStore.open.
                    with pytest.raises(LostLease):
                        self.agents.load(task)
                else:
                    self.invariants.fenced(self.agents, task)
