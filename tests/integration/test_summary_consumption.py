import json

import pytest
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_durable_summary import (
    PRIVATE,
    PUBLIC,
    SummaryProvider,
    setup_summary,
    summary_worker,
)

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.infrastructure.models.mock import MockProvider


def read_summary():
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "read",
                "type": "function",
                "function": {"name": "read_verified_summary", "arguments": "{}"},
            }
        ],
    }


async def consumer_fixture(
    store, *, summary_options=None, transform=None, agent_config=None, **policy
):
    run, agents, _ = await setup_summary(
        store,
        downstream=True,
        downstream_agent={
            **(agent_config or {}),
            "summary_consumption": {
                "mode": "verified_tool",
                "source_summary_step": "summary",
                **policy,
            },
        },
        **(summary_options or {}),
    )
    assert await summary_worker(
        store, agents, SummaryProvider(transform=transform)
    ).tick()
    return run, agents


def consumer_worker(store, agents, provider):
    handlers = HandlerRegistry()

    async def consume(task):
        return await DurableAgentRunner(
            agents,
            provider,
            ToolRuntime(),
            admission=agents if agents.admission_policy else None,
        ).run(task, "consume", allowed=set())

    handlers.register("agent", consume)
    return Worker(store, handlers, lease_seconds=300)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [False, True])
async def test_verified_summary_uses_real_tool_and_explicit_text_authority(
    store, runtime_invariants, text
):
    run, agents = await consumer_fixture(store, allow_claim_text=text)
    before = runtime_invariants(store, run)
    provider = MockProvider([read_summary(), final("done")])
    assert await consumer_worker(store, agents, provider).tick()
    states = runtime_invariants(store, run)
    consumer = states["next"].agent
    assert consumer is not None and consumer.snapshot.status == "succeeded", states[
        "next"
    ].step["error"]
    assert consumer.snapshot.budget.tool_calls == 1
    assert len(consumer.actions) == 1 and consumer.actions[0].tool_binding is not None
    assert states["source"].agent == before["source"].agent
    assert states["summary"].agent == before["summary"].agent
    assert PRIVATE not in json.dumps(provider.contexts)
    view = consumer.actions[0].result
    assert view["assurance"] == "generated_not_fact_verified"
    assert view["source_completeness"] == "selected_evidence_only"
    assert all(("text" in claim) == text for claim in view["selected_claims"])
    for message in provider.contexts[-1]:
        if message["role"] != "tool":
            assert PUBLIC not in message.get("content", "")
    assert (
        consumer.model_attempts[-1].context_projection.summary_consumption_digest
        == view["view_digest"]
    )
    assert (
        consumer.model_attempts[-1].request_contract.summary_consumption_digest
        == view["view_digest"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [False, True])
async def test_generated_instructions_stay_tool_data_and_never_approval(
    store, runtime_invariants, text
):
    def transform(response):
        draft = json.loads(response["content"])
        draft["claims"][0]["text"] = PUBLIC
        return final(json.dumps(draft))

    run, agents = await consumer_fixture(
        store, transform=transform, allow_claim_text=text
    )
    provider = MockProvider([read_summary(), final("done")])
    await consumer_worker(store, agents, provider).tick()
    state = runtime_invariants(store, run)["next"].agent
    assert state.snapshot.status == "succeeded" and not state.approvals
    assert (PUBLIC in json.dumps(provider.contexts)) == text
    assert all(
        PUBLIC not in m.get("content", "")
        for m in provider.contexts[-1]
        if m["role"] != "tool"
    )
    assert (
        len(state.actions) == 1
        and state.actions[0].action.tool_name == "read_verified_summary"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_uncertainty_coverage_and_omissions_are_not_upgraded(
    store, runtime_invariants, allowed
):
    def transform(response):
        draft = json.loads(response["content"])
        claim = draft["claims"][0]
        claim["uncertainty"] = "unknown"
        claim["evidence_references"] = claim["evidence_references"][:1]
        return final(json.dumps(draft))

    run, agents = await consumer_fixture(
        store,
        transform=transform,
        summary_options={
            "allow_uncertain_claims": True,
            "required_evidence_coverage": 0.5,
        },
        **(
            {
                "allowed_uncertainty": ["unknown"],
                "minimum_selected_evidence_coverage": 0.5,
            }
            if allowed
            else {}
        ),
    )
    provider = MockProvider([read_summary(), final("done")])
    await consumer_worker(store, agents, provider).tick()
    if allowed:
        view = runtime_invariants(store, run)["next"].agent.actions[0].result
        assert view["selected_claims"][0]["uncertainty"] == "unknown"
        assert view["selected_evidence_coverage"] == 0.5
        assert view["source_completeness"] == "selected_evidence_only"
    else:
        assert provider.calls == 0


@pytest.mark.asyncio
async def test_consumption_keeps_ordinary_tool_egress_and_projection(
    store, runtime_invariants
):
    from agentflow.application.agents.summary_consumption import consumption_tool
    from agentflow.application.ports.summary_consumption import SummaryConsumptionPolicy

    policy = SummaryConsumptionPolicy(mode="verified_tool", allow_claim_text=True)
    tool = consumption_tool(policy)
    runtime = ToolRuntime()
    runtime.register(tool)
    rules = [
        {
            "tool_name": tool.name,
            "binding": runtime.binding(tool.name).model_dump(mode="json"),
            "result_path": [],
            "mode": "approved_excerpt",
            "classification": "public",
        }
    ]
    run, agents = await consumer_fixture(
        store,
        allow_claim_text=True,
        agent_config={
            "provider_context_egress": {
                "disclosure_mode": "bound_disclosure",
                "max_disclosure_bytes": 4096,
                "rules": rules,
            },
            "context_projection": {"mode": "recent_complete_rounds"},
        },
    )
    from agentflow.application.tools.runtime import Tool
    from agentflow.domain.agent.status import ToolSideEffect

    ordinary_runtime = ToolRuntime()

    async def ordinary(_):
        return {"private": PRIVATE}

    ordinary_runtime.register(
        Tool(
            "ordinary",
            "ordinary",
            {"type": "object"},
            ordinary,
            side_effect=ToolSideEffect.READ_ONLY,
        )
    )
    batch = read_summary()
    batch["tool_calls"].append(
        {
            "id": "ordinary",
            "type": "function",
            "function": {"name": "ordinary", "arguments": "{}"},
        }
    )
    provider = MockProvider([batch, final("done")])
    handlers = HandlerRegistry()

    async def consume(task):
        return await DurableAgentRunner(agents, provider, ordinary_runtime).run(
            task, "consume", allowed={"ordinary"}
        )

    handlers.register("agent", consume)
    await Worker(store, handlers, lease_seconds=300).tick()
    state = runtime_invariants(store, run)["next"].agent
    assert state.snapshot.status == "succeeded", runtime_invariants(store, run)[
        "next"
    ].step["error"]
    assert state.model_attempts[-1].context_projection.egress is not None
    assert "Generated claim" in json.dumps(provider.contexts[-1])
    assert len(state.actions) == 2
    assert PRIVATE in json.dumps(state.actions[1].result)
    assert PRIVATE not in json.dumps(provider.contexts)


@pytest.mark.asyncio
async def test_explicit_metadata_egress_cannot_be_widened_by_consumption(store):
    run, agents = await consumer_fixture(
        store,
        allow_claim_text=True,
        agent_config={"provider_context_egress": {"disclosure_mode": "metadata_only"}},
    )
    provider = MockProvider([read_summary()])
    await consumer_worker(store, agents, provider).tick()
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_metadata_does_not_disclose_model_selected_claim_identifier(store):
    marker = "SYNTHETIC_PRIVATE_IDENTIFIER"

    def transform(response):
        draft = json.loads(response["content"])
        draft["claims"][0]["claim_id"] = marker
        return final(json.dumps(draft))

    _, agents = await consumer_fixture(store, transform=transform)
    provider = MockProvider([read_summary(), final("done")])
    await consumer_worker(store, agents, provider).tick()
    assert marker not in json.dumps(provider.contexts)


@pytest.mark.asyncio
async def test_consumption_keeps_existing_runner_output_validation(store):
    run, agents = await consumer_fixture(store)

    class Validated(DurableAgentRunner):
        def _final_output(self, state, response):
            return {"validated_output": True}

    handlers = HandlerRegistry()

    async def consume(task):
        return await Validated(
            agents, MockProvider([read_summary(), final("done")]), ToolRuntime()
        ).run(task, "consume", allowed=set())

    handlers.register("agent", consume)
    await Worker(store, handlers, lease_seconds=300).tick()
    from sqlalchemy import select

    from agentflow.infrastructure.persistence.models import StepRow

    with store.transaction() as db:
        step = db.scalar(
            select(StepRow).where(StepRow.run_id == run, StepRow.key == "next")
        )
        assert step.output == {"validated_output": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("permit_partial", [False, True])
async def test_consumer_coverage_is_measured_after_claim_filtering(
    store, runtime_invariants, permit_partial
):
    def transform(response):
        draft = json.loads(response["content"])
        first = draft["claims"][0]
        second = {
            **first,
            "claim_id": "uncertain-claim",
            "uncertainty": "unknown",
            "evidence_references": first["evidence_references"][1:],
        }
        first["evidence_references"] = first["evidence_references"][:1]
        draft["claims"].append(second)
        return final(json.dumps(draft))

    run, agents = await consumer_fixture(
        store,
        transform=transform,
        summary_options={"allow_uncertain_claims": True},
        **({"minimum_selected_evidence_coverage": 0.5} if permit_partial else {}),
    )
    provider = MockProvider([read_summary(), final("done")])
    await consumer_worker(store, agents, provider).tick()
    if permit_partial:
        view = runtime_invariants(store, run)["next"].agent.actions[0].result
        assert view["selected_evidence_coverage"] == 0.5
        assert view["summary_evidence_coverage"] == 1.0
        assert view["omitted_claim_count"] == 1
    else:
        assert provider.calls == 0
