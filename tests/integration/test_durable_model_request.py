from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_durable_context_projection import projected_reservation
from test_model_governance import due, submit, worker
from test_observed_usage_governance import reported, usage_policy
from test_provider_admission import opened, policy
from test_provider_admission import worker as admitted_worker

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.projection import project_context
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.application.ports.context_projection import ContextProjectionPolicy
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.ports.model_request import (
    ModelRequestError,
    ModelRequestOptions,
    ProviderCapabilities,
    TokenCount,
)
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentModelAttemptRow
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.asyncio
async def test_retry_contract_is_atomic_stable_and_recovered(store, runtime_invariants):
    run = submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    before = runtime_invariants(store, run)["work"].agent
    assert before.model_attempts[0].request_contract is not None
    due(store, run)
    store.engine.dispose()
    assert await worker(store, provider).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert [a.attempt_index for a in after.model_attempts] == [1, 2]
    assert len({a.model_call_id for a in after.model_attempts}) == 1
    assert (
        after.model_attempts[0].request_contract
        == after.model_attempts[1].request_contract
    )
    assert after.snapshot.budget.model_attempts == provider.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "provider", "output", "tools"])
async def test_deployment_drift_cannot_retry_or_reserve_another_permit(
    store, runtime_invariants, change
):
    config = policy()
    run = submit(store)
    first = MockProvider([ModelRetryableError()])
    assert await admitted_worker(store, first, config).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    kwargs = (
        {"model": "mock-v2"}
        if change == "model"
        else {"provider_key": "mock-new"}
        if change == "provider"
        else {"request_options": ModelRequestOptions(max_output_tokens=128)}
        if change == "output"
        else {
            "capabilities": ProviderCapabilities(
                provider_key="mock",
                model="mock-v1",
                capability_version="2",
                supports_tools=False,
                max_output_tokens=4096,
            )
        }
    )
    provider = MockProvider([final()], **kwargs)
    assert await admitted_worker(store, provider, config).tick()
    after = runtime_invariants(store, run)["work"]
    assert after.step["error"] == "ModelRequestContractChanged"
    assert provider.calls == 0 and after.agent.snapshot.status == "failed"
    assert after.agent.model_attempts == before.model_attempts
    assert after.agent.model_permits == before.model_permits
    assert after.agent.snapshot.budget == before.snapshot.budget


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tools", "strict", "window", "bytes", "output"])
async def test_preflight_rejects_before_budget_attempt_or_permit(
    store, runtime_invariants, case
):
    options = ModelRequestOptions()
    capabilities = None
    counter = None
    if case == "tools":
        capabilities = ProviderCapabilities(
            provider_key="mock",
            model="mock-v1",
            capability_version="1",
            supports_tools=False,
            max_output_tokens=4096,
        )
    elif case == "strict":
        options = ModelRequestOptions(strict_token_window=True)
    elif case == "output":
        options = ModelRequestOptions(max_output_tokens=65537)
    elif case == "window":
        from token_counter_fixtures import profile as counter_profile

        capabilities = ProviderCapabilities(
            provider_key="mock",
            model="mock-v1",
            capability_version="1",
            supports_tools=True,
            max_output_tokens=100,
            context_window_tokens=100,
            token_counting_method="fixture-v1",
            token_counting_assurance="exact",
        )
        options = ModelRequestOptions(max_output_tokens=11, strict_token_window=True)

        class Counter:
            profile = counter_profile()

            def count(self, _):
                return TokenCount(
                    input_tokens=90, method="fixture-v1", assurance="exact"
                )

        counter = Counter()
    run = submit(store, **({"max_context_bytes": 1} if case == "bytes" else {}))

    async def invoke(_):
        pytest.fail("preflight must not dispatch a tool")

    provider = MockProvider(
        request_options=options, capabilities=capabilities, token_counter=counter
    )
    assert await admitted_worker(
        store, provider, policy(), runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"]
    assert (
        state.step["error"]
        == {
            "tools": "ModelCapabilityUnsupported",
            "strict": "ModelTokenCountUnavailable",
            "window": "ModelTokenWindowExceeded",
            "bytes": "AgentContextLimit",
            "output": "ModelOutputLimit",
        }[case]
    )
    assert provider.calls == 0 and state.agent.snapshot.budget.model_attempts == 0
    assert state.agent.model_attempts == state.agent.model_permits == ()
    assert not state.agent.actions


@pytest.mark.asyncio
async def test_request_contract_does_not_bypass_usage_governance(
    store, runtime_invariants
):
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=1).model_dump()
    )
    provider = MockProvider([reported(proposal(), {"total_tokens": 1}), final()])

    async def invoke(_):
        return {"value": 1}

    assert await admitted_worker(
        store, provider, policy(), runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"]
    assert state.step["error"] == "AgentObservedUsageLimit" and provider.calls == 1
    assert len(state.agent.model_attempts) == len(state.agent.model_permits) == 1
    assert state.agent.model_attempts[0].request_contract is not None


@pytest.mark.asyncio
async def test_legacy_failed_projection_without_contract_is_not_retried(
    store, runtime_invariants
):
    run = submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    with store.transaction() as db:
        db.execute(update(AgentModelAttemptRow).values(request_contract=None))
    due(store, run)
    assert await worker(store, provider).tick()
    state = runtime_invariants(store, run)["work"]
    assert state.step["error"] == "ModelRequestContractUnavailable"
    assert provider.calls == 1 and len(state.agent.model_attempts) == 1


@pytest.mark.parametrize("with_projection", [False, True])
def test_store_cannot_retry_unprovable_legacy_request(store, with_projection):
    from test_agent_store import candidate
    from test_provider_admission import reservation

    agents = SQLAgentStore(store)
    task, ready = opened(agents)
    reserve = projected_reservation if with_projection else reservation
    inflight = agents.commit_transition(task, reserve(ready))
    failed = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(inflight.snapshot, status="ready_model"),
            model_result=ModelCallResult(
                model_call_id=inflight.snapshot.model_call_id, status="failed"
            ),
        ),
    )
    with pytest.raises(ModelRequestError, match="ModelRequestContractUnavailable"):
        agents.commit_transition(task, reserve(failed))
    assert agents.load(task) == failed


def reservation_with_contract(state, *, profiled=False):
    transition = projected_reservation(state)
    full = AgentContextBuilder().build(state)
    manifest = project_context(full, [], ContextProjectionPolicy(), 1_000_000).manifest
    provider = MockProvider()
    if profiled:
        from token_counter_fixtures import Counter, capabilities

        provider = MockProvider(
            capabilities=capabilities(),
            token_counter=Counter(),
            request_options=ModelRequestOptions(
                max_output_tokens=10, strict_token_window=True
            ),
        )
    contract = provider.prepare_request(full, [], manifest).request.contract
    return AgentTransition.model_validate(
        {**transition.model_dump(), "request_contract": contract.model_dump()}
    )


def test_new_unprofiled_known_claim_is_readable_but_cannot_reserve(store):
    from agentflow.application.ports.model_request import (
        ModelRequestContract,
        request_digest,
    )

    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    transition = reservation_with_contract(ready, profiled=True)
    values = transition.request_contract.model_dump(mode="json")
    values.pop("token_counter_profile")
    values.pop("request_contract_digest")
    values["request_contract_version"] = 1
    legacy = ModelRequestContract(
        **values, request_contract_digest=request_digest(values)
    )
    candidate = AgentTransition.model_validate(
        {**transition.model_dump(), "request_contract": legacy}
    )
    with pytest.raises(ModelRequestError, match="ModelTokenCounterInvalid"):
        agents.reserve_model_attempt(task, candidate)
    assert agents.load(task) == ready


@pytest.mark.parametrize("field", ["context_projection_digest", "tool_schema_digest"])
def test_store_rejects_forged_contract_before_permit(store, monkeypatch, field):
    from agentflow.application.ports.model_request import request_digest

    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    transition = reservation_with_contract(ready)
    data = transition.model_dump()
    contract = data["request_contract"]
    contract[field] = "0" * 64
    contract["request_contract_digest"] = request_digest(
        {k: v for k, v in contract.items() if k != "request_contract_digest"}
    )

    def forbidden(*_):
        pytest.fail("invalid request must not acquire quota")

    monkeypatch.setattr(agents.admission, "acquire", forbidden)
    with pytest.raises(ModelRequestError):
        agents.reserve_model_attempt(task, AgentTransition.model_validate(data))
    assert agents.load(task) == ready


@pytest.mark.parametrize("profiled", [False, True])
def test_request_rolls_back_and_old_token_cannot_reserve(store, profiled):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    transition = reservation_with_contract(ready, profiled=profiled)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agent_model_attempts"):
            raise OperationalError("injected failure", None, None)

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.reserve_model_attempt(task, transition)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    assert agents.load(task) == ready
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="old"), transition)


@pytest.mark.parametrize("profiled", [False, True])
def test_contract_capacity_reserves_unknown_cancellation_space(
    store, monkeypatch, profiled
):
    from test_action_reviews import fill_to_admission_limit

    import agentflow.infrastructure.persistence.agent_store as module

    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 120_000)
    agents = SQLAgentStore(store)
    task, state = opened(agents)
    state = agents.commit_transition(
        task, reservation_with_contract(state, profiled=profiled)
    )
    state = fill_to_admission_limit(agents, task, state)
    contract = state.model_attempts[0].request_contract
    store.cancel(task.run_id)
    with agents._operator_transaction(state.snapshot.id) as (_, _, _, _, restored):
        assert restored.snapshot.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"
        assert restored.model_attempts[0].request_contract == contract


@pytest.mark.asyncio
async def test_reservation_uses_frozen_contract_and_rejects_missing_strict_interface(
    store, runtime_invariants, monkeypatch
):
    class Legacy:
        async def respond(self, *_):
            pytest.fail(
                "legacy respond-only provider must not dispatch in durable mode"
            )

    run = submit(store)
    assert await worker(store, Legacy()).tick()
    state = runtime_invariants(store, run)["work"]
    assert state.step["error"] == "ModelRequestContractUnavailable"
    assert not state.agent.model_attempts


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
@pytest.mark.parametrize("profiled", [False, True])
def test_independent_postgres_request_reservations_are_fenced(store, profiled):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    transition = reservation_with_contract(ready, profiled=profiled)
    barrier = Barrier(2, timeout=10)

    def reserve(_):
        separate = Store(store.engine.url.render_as_string(hide_password=False))
        try:
            barrier.wait()
            return SQLAgentStore(
                separate, admission_policy=policy()
            ).reserve_model_attempt(task, transition)
        except StateConflict:
            return None
        finally:
            separate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, i) for i in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert sum(r is not None for r in results) == 1
    saved = agents.load(task)
    assert len(saved.model_attempts) == len(saved.model_permits) == 1
    assert saved.model_attempts[0].request_contract == transition.request_contract


@pytest.mark.asyncio
async def test_session_profile_cannot_change_between_logical_rounds(
    store, runtime_invariants
):
    run = submit(store)
    provider = MockProvider([proposal(), final()])

    async def invoke(_):
        provider.model = "changed-deployment"
        return {"value": 1}

    assert await worker(store, provider, runtime=tools(invoke)).tick()
    state = runtime_invariants(store, run)["work"]
    assert state.step["error"] == "ModelSessionConfigurationChanged"
    assert provider.calls == 1 and len(state.agent.model_attempts) == 1
    assert state.agent.actions[0].action.status == "succeeded"
    assert state.agent.snapshot.budget.tool_calls == 1
