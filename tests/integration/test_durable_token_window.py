import asyncio
import json
from copy import deepcopy
from threading import Event

import pytest
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_durable_evidence_checkpoint import CONFIG
from test_durable_summary import SummaryProvider, setup_summary, summary_worker
from test_model_governance import due, submit, worker
from test_observed_usage_governance import reported, usage_policy
from test_provider_admission import opened, policy, reservation
from test_provider_admission import worker as admitted_worker
from test_summary_consumption import consumer_fixture, consumer_worker, read_summary
from token_counter_fixtures import Counter, capabilities, profile

from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelRetryableError,
)
from agentflow.application.ports.model_request import (
    ModelRequestOptions,
    request_digest,
)
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore


def counted(provider=None, counter=None):
    provider = provider or MockProvider([final()])
    provider.token_counter = counter or Counter()
    provider.capabilities = capabilities()
    provider.request_options = ModelRequestOptions(
        max_output_tokens=10, strict_token_window=True
    )
    return provider


def assert_frozen_counts(provider, session):
    assert len(provider.token_counter.payloads) == len(session.model_attempts)
    for wire, messages, attempt in zip(
        provider.token_counter.payloads,
        provider.contexts,
        session.model_attempts,
        strict=True,
    ):
        payload = json.loads(wire)
        contract = attempt.request_contract
        assert payload["messages"] == messages
        assert contract.request_contract_version == 2
        assert contract.token_counter_profile == provider.token_counter.profile
        assert (
            request_digest(
                {
                    "messages": messages,
                    **({"tools": payload["tools"]} if payload.get("tools") else {}),
                }
            )
            == contract.context_projection_digest
        )
        assert (
            contract.token_count.input_tokens + contract.reserved_output_tokens == 100
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "recent", "checkpoint", "egress"])
async def test_count_only_final_projected_disclosed_wire_once_per_attempt(
    store, runtime_invariants, mode
):
    config = deepcopy(CONFIG) if mode == "checkpoint" else {}
    if mode in {"recent", "checkpoint"}:
        config["max_context_bytes"] = 6200
        if mode == "recent":
            config["context_projection"] = {"mode": "recent_complete_rounds"}
    if mode == "egress":
        config["provider_context_egress"] = {"disclosure_mode": "metadata_only"}
    run = submit(store, **config)
    provider = counted(MockProvider([proposal(), proposal(), proposal(), final()]))

    async def invoke(_):
        return {
            "value": "公开数据" * (100 if mode == "checkpoint" else 300),
            "private": "PRIVATE_WIRE_OMITTED",
        }

    assert await worker(store, provider, runtime=tools(invoke)).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded"
    assert_frozen_counts(provider, state)
    assert all(a.result["private"] == "PRIVATE_WIRE_OMITTED" for a in state.actions)
    if mode == "recent":
        assert state.model_attempts[-1].context_projection.omitted_round_count > 0
    if mode == "egress":
        assert "PRIVATE_WIRE_OMITTED" not in "".join(provider.token_counter.payloads)


@pytest.mark.asyncio
async def test_summary_generator_has_independent_counts(store, runtime_invariants):
    run, agents, _ = await setup_summary(store)
    generator = counted(SummaryProvider())
    assert await summary_worker(store, agents, generator).tick()
    state = runtime_invariants(store, run)
    assert_frozen_counts(generator, state["summary"].agent)
    assert len(generator.token_counter.payloads) == 2
    assert all(
        a.request_contract.summary_input_digest
        for a in state["summary"].agent.model_attempts
    )
    assert all(
        a.request_contract.request_contract_version == 1
        for a in state["source"].agent.model_attempts
    )


@pytest.mark.asyncio
async def test_verified_consumer_has_independent_counts(store, runtime_invariants):
    run, agents = await consumer_fixture(store)
    before = runtime_invariants(store, run)
    consumer = counted(MockProvider([read_summary(), final()]))
    assert await consumer_worker(store, agents, consumer).tick()
    after = runtime_invariants(store, run)
    assert_frozen_counts(consumer, after["next"].agent)
    assert all(
        a.request_contract.summary_consumption_digest
        for a in after["next"].agent.model_attempts
    )
    assert after["summary"].agent == before["summary"].agent


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_retry_reads_durable_profile_after_reconnect(
    store, runtime_invariants, drift
):
    run = submit(store)
    first = counted(MockProvider([ModelRetryableError()]))
    assert await admitted_worker(store, first, policy()).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    store.engine.dispose()
    counter = (
        Counter(profile=profile(chat_template_version="2")) if drift else Counter()
    )
    second = counted(counter=counter)
    assert await admitted_worker(store, second, policy()).tick()
    after = runtime_invariants(store, run)["work"]
    if drift:
        assert after.step["error"] == "ModelRequestContractChanged"
        assert after.agent.model_attempts == before.model_attempts
        assert after.agent.model_permits == before.model_permits
        assert second.calls == 0
    else:
        assert after.agent.snapshot.status == "succeeded"
        assert (
            after.agent.model_attempts[0].model_call_id
            == after.agent.model_attempts[1].model_call_id
        )
        assert (
            after.agent.model_attempts[0].request_contract
            == after.agent.model_attempts[1].request_contract
        )
        assert after.agent.snapshot.budget.model_attempts == 2


@pytest.mark.asyncio
async def test_count_failure_and_profile_mismatch_never_reserve(
    store, runtime_invariants
):
    class Broken(Counter):
        def count(self, _):
            raise RuntimeError("PRIVATE_TOKEN_COUNT_ERROR")

    for counter in (Broken(), Counter(profile=profile(model="other-model"))):
        run = submit(store)
        provider = counted(counter=counter)
        assert await admitted_worker(store, provider, policy()).tick()
        state = runtime_invariants(store, run)["work"]
        assert state.step["error"] == "ModelTokenCounterInvalid"
        assert state.agent.model_attempts == state.agent.model_permits == ()
        assert state.agent.snapshot.budget.model_attempts == provider.calls == 0


@pytest.mark.asyncio
async def test_count_cannot_bypass_admission_or_observed_usage(
    store, runtime_invariants
):
    # Keep the first deferred Run unavailable while checking a separate pool.
    # No assertion here depends on completing DB I/O within the default 2 s.
    config = policy(admission_retry_delay_seconds=3600)
    agents = SQLAgentStore(store, admission_policy=config)
    task, state = opened(agents)
    agents.reserve_model_attempt(task, reservation(state))
    run = submit(store)
    provider = counted()
    assert await admitted_worker(store, provider, config).tick()
    deferred = runtime_invariants(store, run)["work"]
    assert deferred.step["error"] == "ProviderAdmissionDeferred"
    assert deferred.agent.model_attempts == deferred.agent.model_permits == ()
    assert provider.calls == 0

    # Another isolated provider policy avoids inheriting the first permit.
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=1).model_dump()
    )
    provider = counted(
        MockProvider([reported(proposal(), {"total_tokens": 1}), final()])
    )

    async def invoke(_):
        return {"value": "public"}

    assert await admitted_worker(
        store,
        provider,
        policy(provider_key="another"),
        runtime=tools(invoke),
        allowed={"echo"},
    ).tick()
    limited = runtime_invariants(store, run)["work"]
    assert limited.step["error"] == "AgentObservedUsageLimit"
    assert (
        len(limited.agent.model_attempts)
        == len(limited.agent.model_permits)
        == provider.calls
        == 1
    )


@pytest.mark.asyncio
async def test_unknown_counted_request_is_not_resent(store, runtime_invariants):
    run = submit(store)
    provider = counted(MockProvider([ModelOutcomeUnknown(), final()]))
    assert await admitted_worker(store, provider, policy()).tick()
    store.engine.dispose()
    assert not await admitted_worker(store, provider, policy()).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert (
        state.snapshot.status == "failed"
        and state.model_attempts[0].outcome == "unknown"
    )
    assert state.model_attempts[0].usage.total_tokens is None
    assert provider.calls == len(state.model_attempts) == 1


@pytest.mark.asyncio
async def test_slow_pure_counter_does_not_block_loop_or_reserve_after_cancel(
    store, runtime_invariants
):
    started, release = Event(), Event()

    class Slow(Counter):
        def count(self, payload):
            started.set()
            assert release.wait(5)
            return super().count(payload)

    run = submit(store)
    provider = counted(counter=Slow())
    running = asyncio.create_task(worker(store, provider).tick())
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts == state.model_permits == ()
    assert provider.calls == 0
