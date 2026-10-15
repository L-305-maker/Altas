import pytest
from pydantic import ValidationError

from agentflow.domain.agent.budget import AgentBudget, AgentBudgetExhaustedError


@pytest.fixture
def budget():
    return AgentBudget(
        max_model_rounds=2,
        max_model_attempts=3,
        max_tool_calls=2,
        max_resource_units=100,
    )


def test_budget_accumulates_attempts_independently_and_restores(budget):
    started = budget.consume(model_rounds=1, model_attempts=1, resource_units=20)
    retried = started.consume(model_attempts=1, resource_units=10)
    restored = AgentBudget.model_validate_json(retried.model_dump_json())
    final = restored.consume(
        model_rounds=1, model_attempts=1, tool_calls=2, resource_units=70
    )
    assert (final.model_rounds, final.model_attempts) == (2, 3)
    assert (final.tool_calls, final.resource_units) == (2, 100)
    assert budget.model_rounds == budget.resource_units == 0


@pytest.mark.parametrize(
    "charges,resource",
    [
        ({"model_rounds": 3, "model_attempts": 3}, "model_rounds"),
        ({"model_attempts": 4}, "model_attempts"),
        ({"tool_calls": 3}, "tool_calls"),
        ({"resource_units": 101}, "resource_units"),
    ],
)
def test_budget_exhaustion_has_a_distinct_error_and_is_atomic(
    budget, charges, resource
):
    original = budget.model_dump_json()
    with pytest.raises(AgentBudgetExhaustedError) as caught:
        budget.consume(**charges)
    assert caught.value.resource == resource
    assert budget.model_dump_json() == original


@pytest.mark.parametrize("resource", ["model_attempts", "tool_calls", "resource_units"])
def test_exact_limit_is_usable_but_next_charge_fails(budget, resource):
    full = budget.consume(**{resource: getattr(budget, f"max_{resource}")})
    assert full.consume() == full
    with pytest.raises(AgentBudgetExhaustedError):
        full.consume(**{resource: 1})


def test_zero_budget_disables_operations():
    disabled = AgentBudget(
        max_model_rounds=0,
        max_model_attempts=0,
        max_tool_calls=0,
        max_resource_units=0,
    )
    assert disabled.consume() == disabled
    with pytest.raises(AgentBudgetExhaustedError):
        disabled.consume(model_attempts=1)


@pytest.mark.parametrize("value", [-1, True, 0.5, float("inf"), "1"])
@pytest.mark.parametrize(
    "resource", ["model_rounds", "model_attempts", "tool_calls", "resource_units"]
)
def test_budget_cannot_refund_or_coerce_charges(budget, resource, value):
    with pytest.raises(ValueError):
        budget.consume(**{resource: value})
    with pytest.raises(ValidationError):
        AgentBudget.model_validate({**budget.model_dump(), resource: value})
    with pytest.raises(ValidationError):
        AgentBudget.model_validate({**budget.model_dump(), f"max_{resource}": value})


@pytest.mark.parametrize(
    "changes",
    [
        {"model_rounds": 1, "model_attempts": 0},
        {"model_attempts": 4},
        {"tool_calls": 3},
        {"resource_units": 101},
        {"unknown_counter": 1},
    ],
)
def test_invalid_budget_cannot_be_restored(budget, changes):
    with pytest.raises(ValidationError):
        AgentBudget.model_validate({**budget.model_dump(), **changes})


def test_model_round_requires_an_attempt(budget):
    with pytest.raises(ValidationError):
        budget.consume(model_rounds=1)
