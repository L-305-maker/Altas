import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_model_governance import due, submit

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import Base, RunRow

LEGACY_V2 = Path(__file__).resolve().parents[1] / "fixtures/checkpoint_v2_918ae92.json"


def restore_v2(store):
    """Captured by the actual 918ae92 runtime before any v3 implementation."""
    captured = json.loads(LEGACY_V2.read_text(encoding="utf-8"))
    with store.engine.begin() as db:
        for table in Base.metadata.sorted_tables:
            if captured["tables"][table.name]:
                db.execute(table.insert(), captured["tables"][table.name])
        if store.engine.dialect.name == "postgresql":
            db.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('events', 'id'), (SELECT max(id) FROM events))"
                )
            )
    return captured


def read_runtime(**changes):
    async def never(_):
        pytest.fail("historical result must not execute again")

    runtime = ToolRuntime()
    runtime.register(
        Tool(
            "read",
            "read",
            {"type": "object"},
            never,
            side_effect=ToolSideEffect.READ_ONLY,
            version=changes.pop("version", "v2"),
            **changes,
        )
    )
    return runtime


def v3_config(runtime):
    return {
        "context_projection": {"mode": "evidence_anchored_recent_rounds"},
        "evidence_checkpoint": {
            "mode": "anchored_extracts",
            "checkpoint_version": 3,
            "selection_policy": "pinned_then_recent",
            "disclosure": {
                "policy_version": 2,
                "rules": [
                    {
                        "tool_name": "read",
                        "result_path": ["value"],
                        "binding": runtime.binding("read").model_dump(mode="json"),
                        "mode": "approved_excerpt",
                        "classification": "public",
                    }
                ],
            },
        },
    }


def executor(store, provider, runtime, *, agents=None, admission=None):
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            agents or SQLAgentStore(store), provider, runtime, admission=admission
        ).run(task, "captured synthetic goal", allowed={"read"})

    handlers.register("agent", handler)
    return Worker(store, handlers, lease_seconds=300)


def read_proposal():
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "provider-local",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_v2_name_only_authorization_cannot_replay_private_upgraded_tool_result(
    store, runtime_invariants
):
    captured = restore_v2(store)
    run = captured["scenarios"]["public_retry"]
    before = runtime_invariants(store, run)["work"].agent
    assert [a.tool_binding.tool_version for a in before.actions] == ["v1", "v2"]
    assert len({a.tool_binding.tool_policy_digest for a in before.actions}) == 2
    due(store, run)
    task = store.claim(300)
    assert task.run_id == run
    provider = MockProvider([final()])
    result = await DurableAgentRunner(
        SQLAgentStore(store), provider, read_runtime()
    ).run(task, "captured synthetic goal", allowed={"read"})
    if "SYNTHETIC_V2_PRIVATE_VALUE" in json.dumps(provider.contexts):
        pytest.fail("ToolVersionDisclosureNotAuthorized", pytrace=False)
    assert result.status == "failed" and provider.calls == 0
    after = runtime_invariants(store, run)["work"]
    assert after.step["error"] == "AgentEvidenceV2PublicReadOnly"
    assert after.agent.model_attempts == before.model_attempts
    assert (
        after.agent.messages == before.messages
        and after.agent.actions == before.actions
    )
    assert after.agent.snapshot.budget == before.snapshot.budget


def test_captured_v2_requests_remain_byte_verifiable(store):
    from agentflow.application.agents.checkpoint import build_checkpoint
    from agentflow.application.agents.context import AgentContextBuilder
    from agentflow.application.agents.projection import project_context
    from agentflow.application.ports.context_projection import ContextProjectionPolicy
    from agentflow.application.ports.evidence_checkpoint import EvidenceCheckpointPolicy

    captured = restore_v2(store)
    checked = 0
    for run in captured["scenarios"].values():
        with store.transaction() as db:
            config = db.get(RunRow, run).spec["steps"][0]["parameters"]["agent"]
            session_id = next(
                s["id"]
                for s in captured["tables"]["agent_sessions"]
                if s["step_row_id"]
                == next(
                    s["id"] for s in captured["tables"]["steps"] if s["run_id"] == run
                )
            )
        with SQLAgentStore(store)._operator_transaction(session_id) as (
            _,
            _,
            _,
            _,
            state,
        ):
            for attempt in state.model_attempts:
                end = next(
                    (
                        m.sequence
                        for m in state.messages
                        if m.role == "assistant"
                        and m.model_call_id == attempt.model_call_id
                    ),
                    len(state.messages),
                )
                prefix = state.messages[:end]
                ids = {
                    i
                    for m in prefix
                    if m.role == "assistant"
                    for i in m.payload["action_ids"]
                }
                historical = type(state).model_validate(
                    {
                        **state.model_dump(),
                        "messages": prefix,
                        "actions": [a for a in state.actions if a.action.id in ids],
                    }
                )
                projected = project_context(
                    AgentContextBuilder().build(historical),
                    read_runtime().schemas({"read"}),
                    ContextProjectionPolicy(**config["context_projection"]),
                    attempt.context_projection.max_context_bytes,
                    checkpoint=build_checkpoint(
                        historical,
                        EvidenceCheckpointPolicy(**config["evidence_checkpoint"]),
                    ),
                )
                assert projected.manifest == attempt.context_projection
                attempt.request_contract.verify_context(
                    projected.messages, read_runtime().schemas({"read"})
                )
                attempt.request_contract.verify_projection(projected.manifest)
                assert all(
                    r.tool_binding_digest is None
                    for r in projected.manifest.checkpoint.references
                )
                checked += 1
    assert checked == 9


@pytest.mark.asyncio
async def test_captured_metadata_only_v2_retry_continues_without_identity_upgrade(
    store, runtime_invariants
):
    captured = restore_v2(store)
    run = captured["scenarios"]["metadata_retry"]
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    task = store.claim(300)
    assert task.run_id == run
    provider = MockProvider([final()])
    result = await DurableAgentRunner(
        SQLAgentStore(store), provider, read_runtime()
    ).run(task, "captured synthetic goal", allowed={"read"})
    after = runtime_invariants(store, run)["work"].agent
    assert result.status == "succeeded" and provider.calls == 1
    assert after.model_attempts[0] == before.model_attempts[0]
    assert (
        after.model_attempts[0].context_projection
        == after.model_attempts[1].context_projection
    )
    assert (
        after.model_attempts[0].request_contract
        == after.model_attempts[1].request_contract
    )
    assert (
        after.snapshot.budget.model_rounds == 1
        and after.snapshot.budget.model_attempts == 2
    )


def test_upgrade_audit_distinguishes_active_public_policy_and_terminal_v2_history(
    store, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit
    from test_model_request_migrations import old_facts

    captured = restore_v2(store)
    before = old_facts(store)
    report = audit(store.engine)
    assert report["v2_checkpoint_sessions"] == 5
    assert report["v2_public_excerpt_policy_sessions"] == 4
    assert report["active_unbound_public_policy_sessions"] == 2
    assert report["operator_review_sessions"] == 2
    assert report["pending_retry_calls"] == 2 and report["retry_recovery_blocked"] == 1
    assert report["v1_checkpoint_sessions"] == report["v3_checkpoint_sessions"] == 0
    assert {"code": "AgentEvidenceV2PublicReadOnly", "count": 2} in report["findings"]
    assert not any(run in json.dumps(report) for run in captured["scenarios"].values())
    assert "SYNTHETIC_V2_PRIVATE_VALUE" not in json.dumps(report)
    assert old_facts(store) == before


@pytest.mark.asyncio
async def test_v3_worker_discloses_only_exact_historical_version_across_rounds(
    store, runtime_invariants
):
    runtime = read_runtime(version="v1")
    config = v3_config(runtime)
    config["evidence_checkpoint"]["pinned_references"] = [
        {
            "source_type": "action",
            "source_sequence": 4,
            "call_index": 0,
            "extraction_path": ["result", "value"],
            "mandatory": False,
        }
    ]
    calls = []

    async def invoke(_):
        calls.append(runtime.binding("read"))
        runtime._tools["read"] = replace(runtime._tools["read"], version="v2")
        return {
            "value": "public-v1"
            if len(calls) == 1
            else "SYNTHETIC_VERSION_PRIVATE_VALUE"
        }

    runtime._tools["read"] = replace(runtime._tools["read"], invoke=invoke)
    run = submit(store, **config)
    provider = MockProvider([read_proposal(), read_proposal(), final()])
    assert await executor(store, provider, runtime).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and provider.calls == 3
    assert len(calls) == len(state.actions) == state.snapshot.budget.tool_calls == 2
    assert [a.tool_binding for a in state.actions] == calls
    assert len({a.action.id for a in state.actions}) == 2
    if "SYNTHETIC_VERSION_PRIVATE_VALUE" in json.dumps(provider.contexts):
        pytest.fail("HistoricalToolBindingDisclosureLeak", pytrace=False)
    assert any(
        "public-v1" in m.get("content", "")
        for m in provider.contexts[-1]
        if m["role"] == "tool"
    )
    references = state.model_attempts[-1].context_projection.checkpoint.references
    assert references[0].action_id == state.actions[1].action.id
    assert references[0].tool_binding_digest is not None
    assert len(store.artifacts(run)) == 1


@pytest.mark.asyncio
async def test_v3_retry_uses_stored_binding_after_registry_version_changes(
    store, runtime_invariants, monkeypatch
):
    runtime = read_runtime(version="v1")

    async def invoke(_):
        return {"value": "approved historical v1"}

    runtime._tools["read"] = replace(runtime._tools["read"], invoke=invoke)
    run = submit(store, **v3_config(runtime))
    first = MockProvider([read_proposal(), ModelRetryableError()])
    assert await executor(store, first, runtime).tick()
    before = runtime_invariants(store, run)["work"].agent
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit

    report = audit(store.engine)
    assert report["v3_checkpoint_sessions"] == 1
    assert (
        report["active_unbound_public_policy_sessions"]
        == report["operator_review_sessions"]
        == 0
    )
    assert report["pending_retry_calls"] == 1 and report["retry_recovery_blocked"] == 0
    due(store, run)
    store.engine.dispose()
    # Current registry is different; the completed Action's historical identity
    # remains authoritative. No tool is called on this continuation.
    provider = MockProvider([final()])
    assert await executor(store, provider, read_runtime(version="v2")).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert after.snapshot.status == "succeeded"
    assert after.actions == before.actions
    assert (
        after.snapshot.budget.model_rounds == 2
        and after.snapshot.budget.model_attempts == 3
    )
    assert after.snapshot.budget.tool_calls == 1
    assert (
        after.model_attempts[-1].model_call_id
        == before.model_attempts[-1].model_call_id
    )
    assert (
        after.model_attempts[-1].context_projection
        == before.model_attempts[-1].context_projection
    )
    assert (
        after.model_attempts[-1].request_contract
        == before.model_attempts[-1].request_contract
    )
    assert any(
        "approved historical v1" in m.get("content", "")
        for m in provider.contexts[0]
        if m["role"] == "tool"
    )


@pytest.mark.asyncio
async def test_v3_reconciliation_keeps_original_binding_and_approval(
    store, runtime_invariants
):
    from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision

    runtime = read_runtime(version="v1", requires_approval=True)
    calls = []

    async def unknown(_):
        calls.append(True)
        raise TimeoutError

    runtime._tools["read"] = replace(runtime._tools["read"], invoke=unknown)
    agents = SQLAgentStore(store)
    run = submit(store, **v3_config(runtime))
    provider = MockProvider([read_proposal(), final()])
    worker = executor(store, provider, runtime, agents=agents)
    assert await worker.tick()
    pending = runtime_invariants(store, run)["work"].agent
    agents.decide_action(
        pending.approvals[0].approval_id,
        ApprovalDecision(
            expected_revision=pending.snapshot.revision,
            decision="approved",
            actor="test",
        ),
    )
    assert await worker.tick()
    waiting = runtime_invariants(store, run)["work"].agent
    assert waiting.snapshot.status == "waiting_reconciliation" and provider.calls == 1
    assert not await worker.tick()
    agents.reconcile_action(
        waiting.snapshot.id,
        waiting.actions[0].action.id,
        ReconciliationDecision(
            request_id="binding-reconcile",
            expected_revision=waiting.snapshot.revision,
            confirmed_status="succeeded",
            result={
                "value": "confirmed public",
                "password": "SYNTHETIC_RECONCILIATION_PRIVATE",
            },
            actor="test",
            evidence_reference="synthetic",
        ),
    )
    assert await worker.tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and calls == [True]
    assert state.actions[0].tool_binding == pending.actions[0].tool_binding
    assert state.actions[0].idempotency_key == pending.actions[0].idempotency_key
    assert (
        state.actions[0].action.arguments_digest
        == pending.actions[0].action.arguments_digest
    )
    assert state.approvals[0].consumed_at is not None
    observations = [
        json.loads(m["content"]) for m in provider.contexts[-1] if m["role"] == "tool"
    ]
    assert len(observations) == 1 and observations[0]["status"] == "succeeded"
    assert observations[0]["result"]["items"][0]["excerpt"] == "confirmed public"
    if "SYNTHETIC_RECONCILIATION_PRIVATE" in json.dumps(provider.contexts):
        pytest.fail("ReconciliationDisclosureLeak", pytrace=False)


@pytest.mark.asyncio
async def test_store_rejects_forged_binding_identity_before_permit(store, monkeypatch):
    from test_provider_admission import policy

    from agentflow.application.agents.durable_runner import successor
    from agentflow.application.ports.agent_store import AgentTransition, LostLease
    from agentflow.application.ports.context_projection import (
        ContextProjectionError,
        ContextProjectionManifest,
        ContextProjectionReservation,
    )
    from agentflow.domain.agent.action import digest_arguments

    runtime = read_runtime(version="v1")

    async def invoke(_):
        return {"value": "public"}

    runtime._tools["read"] = replace(runtime._tools["read"], invoke=invoke)
    config = v3_config(runtime)
    config["evidence_checkpoint"]["pinned_references"] = [
        {
            "source_type": "action",
            "source_sequence": 2,
            "call_index": 0,
            "extraction_path": ["result", "value"],
            "mandatory": False,
        }
    ]
    run = submit(store, **config)
    agents = SQLAgentStore(store, admission_policy=policy())
    first = MockProvider([read_proposal(), ModelRetryableError()])
    assert await executor(store, first, runtime, agents=agents, admission=agents).tick()
    due(store, run)
    task = store.claim(300)
    before = agents.load(task)
    saved = before.model_attempts[-1].context_projection.model_dump()
    checkpoint = saved["checkpoint"]
    assert checkpoint["references"][0]["tool_binding_digest"] is not None
    checkpoint["references"][0]["tool_binding_digest"] = "0" * 64
    checkpoint["evidence_reference_digest"] = digest_arguments(
        {"references": checkpoint["references"]}
    )
    manifest = ContextProjectionManifest.model_validate(saved)
    provider = MockProvider()
    schemas = runtime.schemas({"read"})
    transition = AgentTransition(
        snapshot=successor(
            before.snapshot,
            status="model_inflight",
            actions=(),
            budget=before.snapshot.budget.consume(model_attempts=1),
        ),
        context_projection=ContextProjectionReservation(
            manifest=manifest, tools=tuple(schemas)
        ),
        request_contract=provider.prepare_request(
            first.contexts[-1], schemas, manifest
        ).request.contract,
    )

    def forbidden(*args):
        pytest.fail("forged binding identity cannot acquire a permit")

    monkeypatch.setattr(agents.admission, "acquire", forbidden)
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="stale"), transition)
    with pytest.raises(ContextProjectionError, match="AgentContextProjectionChanged"):
        agents.reserve_model_attempt(task, transition)
    assert provider.calls == 0 and agents.load(task) == before


def test_v2_public_store_guard_cannot_be_bypassed_with_a_v3_caller_policy(
    store, monkeypatch
):
    from test_durable_evidence_checkpoint import evidence_reservation
    from test_provider_admission import policy

    from agentflow.application.agents.durable_runner import DurableAgentConfig
    from agentflow.application.ports.context_projection import ContextProjectionError
    from agentflow.domain.agent.run import AgentSnapshot

    captured = json.loads(LEGACY_V2.read_text(encoding="utf-8"))
    config = next(
        r["spec"]["steps"][0]["parameters"]["agent"]
        for r in captured["tables"]["runs"]
        if r["id"] == captured["scenarios"]["public_retry"]
    )
    run = submit(store, **{k: v for k, v in config.items() if k != "mode"})
    task = store.claim(300)
    assert task.run_id == run
    agents = SQLAgentStore(store, admission_policy=policy())
    before = agents.open(
        task,
        AgentSnapshot(
            id="unreserved-public", budget=DurableAgentConfig().initial_budget()
        ),
    )
    transition = evidence_reservation(
        before, config=v3_config(read_runtime(version="v1"))
    )

    def forbidden(*args):
        pytest.fail("old broad public policy cannot acquire a permit")

    monkeypatch.setattr(agents.admission, "acquire", forbidden)
    with pytest.raises(ContextProjectionError, match="AgentEvidenceV2PublicReadOnly"):
        agents.reserve_model_attempt(task, transition)
    assert agents.load(task) == before
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit

    report = audit(store.engine)
    assert report["v2_checkpoint_sessions"] == 0  # No historical attempt exists yet.
    assert (
        report["v2_public_excerpt_policy_sessions"]
        == report["active_unbound_public_policy_sessions"]
        == 1
    )
    assert (
        report["evidence_reservation_blocked_sessions"]
        == report["operator_review_sessions"]
        == 1
    )
