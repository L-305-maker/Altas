"""Seed v1 request facts with a test-only old adapter, then run current code."""

import json
from pathlib import Path

import httpx
import pytest
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_durable_context_projection import projected_reservation
from test_model_governance import due, submit
from test_provider_admission import opened, policy, worker

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.ports.agent_store import AgentTransition
from agentflow.application.ports.model_provider import (
    ModelResponse,
    ModelRetryableError,
)
from agentflow.application.ports.model_request import (
    PreparedModelRequest,
    ProviderCapabilities,
    build_request,
    request_digest,
)
from agentflow.bootstrap import build_provider
from agentflow.config import Settings
from agentflow.infrastructure.models.deepseek import DeepSeekProvider
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


class V1RecordedProvider(MockProvider):
    """Only a historical fixture; does not bypass current production preflight."""

    def __init__(self, responses, model="deepseek-chat"):
        super().__init__(responses, model=model, provider_key="deployment-label")

    def prepare_request(self, messages, tools, projection):
        request = build_request(
            messages,
            tools,
            projection,
            capabilities=ProviderCapabilities(
                provider_key=self.provider_key,
                model=self.model,
                capability_version="deepseek.adapter.1",
                supports_tools=True,
                supports_json_output=True,
                supported_parameters=("temperature", "top_p"),
                max_output_tokens=4096,
            ),
            options=self.request_options,
            adapter_version="deepseek.chat.v1",
            endpoint_fingerprint=request_digest(
                "https://api.deepseek.com/chat/completions"
            ),
        )

        async def execute():
            payload = request.payload()
            return await self.respond(payload["messages"], payload.get("tools", []))

        return PreparedModelRequest(request, execute)


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_legacy", [False, True])
async def test_deployment_default_never_rewrites_or_replays_old_retry(
    store, runtime_invariants, keep_legacy
):
    run = submit(store)
    assert await worker(
        store, V1RecordedProvider([ModelRetryableError()]), policy()
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    calls = []

    def forbidden(request):
        calls.append(request)
        pytest.fail("HistoricalRequestReplayed", pytrace=False)

    provider = build_provider(
        Settings(
            provider="deepseek",
            deepseek_api_key="synthetic-only",
            model_provider_key="deployment-label",
        )
    )
    provider._transport = httpx.MockTransport(forbidden)
    if keep_legacy:
        provider.model = "deepseek-chat"
    due(store, run)
    assert await worker(store, provider, policy()).tick()
    saved = runtime_invariants(store, run)["work"]
    assert saved.step["error"] == (
        "ModelDeprecated" if keep_legacy else "ModelRequestContractChanged"
    )
    assert not calls
    assert saved.agent.model_attempts == before.model_attempts
    assert saved.agent.model_calls == before.model_calls
    assert saved.agent.model_permits == before.model_permits
    assert saved.agent.snapshot.budget == before.snapshot.budget


@pytest.mark.asyncio
async def test_canonical_v1_retry_retains_identical_contract(store, runtime_invariants):
    run = submit(store)
    assert await worker(
        store,
        V1RecordedProvider([ModelRetryableError()], model="deepseek-flash"),
        policy(),
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": final(), "finish_reason": "stop"}]}
        )

    provider = DeepSeekProvider(
        "synthetic-only",
        "deepseek-flash",
        provider_key="deployment-label",
        transport=httpx.MockTransport(handle),
    )
    due(store, run)
    assert await worker(store, provider, policy()).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert after.snapshot.status == "succeeded" and len(sent) == 1
    assert after.snapshot.budget.model_attempts == 2
    assert after.model_attempts[0] == before.model_attempts[0]
    assert (
        after.model_attempts[0].request_contract
        == after.model_attempts[1].request_contract
    )
    assert (
        after.model_attempts[0].context_projection
        == after.model_attempts[1].context_projection
    )
    assert (
        after.model_attempts[0].model_call_id == after.model_attempts[1].model_call_id
    )


@pytest.mark.asyncio
async def test_lifecycle_audit_counts_legacy_without_changing_history(
    store, runtime_invariants, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit

    waiting = submit(store)
    assert await worker(
        store, V1RecordedProvider([ModelRetryableError()]), policy()
    ).tick()
    completed = submit(store)

    async def read(_):
        return {"value": 1}

    assert await worker(
        store,
        V1RecordedProvider(
            [
                proposal(),
                ModelResponse(
                    assistant_message=final(),
                    finish_reason="stop",
                    usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                ),
            ]
        ),
        policy(),
        runtime=tools(read),
        allowed={"echo"},
    ).tick()
    agents = SQLAgentStore(store)
    task, ready = opened(agents)
    reserve = projected_reservation(ready)
    request = V1RecordedProvider([final()]).prepare_request(
        AgentContextBuilder().build(ready), [], reserve.context_projection.manifest
    )
    agents.commit_transition(
        task,
        AgentTransition.model_validate(
            {**reserve.model_dump(), "request_contract": request.request.contract}
        ),
    )
    before = {
        run: runtime_invariants(store, run) for run in (waiting, completed, task.run_id)
    }
    artifacts = store.artifacts(completed)
    assert (
        before[completed]["work"].agent.model_attempts[-1].raw_usage["total_tokens"]
        == 3
    )
    report = audit(store.engine)
    assert report["legacy_model_sessions"] == 3
    assert report["legacy_pending_retry_calls"] == 1
    assert report["model_inflight"] == 1
    assert report["terminal_historical_calls"] == 2
    assert report["unbound_request_sessions"] == 0
    assert report["operator_review_sessions"] == 2
    assert report["retry_recovery_blocked"] == 1
    for run, facts in before.items():
        assert runtime_invariants(store, run) == facts
        assert run not in json.dumps(report)
    assert store.artifacts(completed) == artifacts
    assert "PRIVATE" not in json.dumps(report)
    with agents._operator_transaction(before[completed]["work"].agent.snapshot.id) as (
        _,
        _,
        _,
        _,
        restored,
    ):
        assert restored == before[completed]["work"].agent
