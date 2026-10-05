import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec


@given(st.integers(min_value=1, max_value=100))
def test_arbitrary_length_chain_validates(length):
    steps = tuple(
        StepSpec(id=f"s{i}", handler="echo", depends_on=(f"s{i - 1}",) if i else ())
        for i in range(length)
    )
    assert len(WorkflowSpec(name="chain", steps=steps).steps) == length


@pytest.mark.parametrize(
    "steps",
    [
        [{"id": "a", "handler": "echo", "depends_on": ["missing"]}],
        [{"id": "a", "handler": "echo", "depends_on": ["a"]}],
        [{"id": "a", "handler": "echo"}, {"id": "a", "handler": "echo"}],
    ],
)
def test_invalid_graph_is_rejected(steps):
    with pytest.raises(ValidationError):
        WorkflowSpec(name="invalid", steps=steps)
