import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agentflow.applications.living_guideline import GuidelineHandlers
from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider


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


@pytest.mark.asyncio
async def test_hallucinated_quote_is_rejected():
    provider = MockProvider(
        [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"quotes": [{"source_id": "invented", "quote": "not in source"}]}
                ),
            }
        ]
    )
    task = Task(
        "run",
        "extract",
        "token",
        1,
        StepSpec(id="extract", handler="guideline.extract"),
        {
            "title": "test",
            "documents": [{"title": "source", "text": "actual source text"}],
        },
        {},
    )
    with pytest.raises(ValueError, match="does not match"):
        await GuidelineHandlers(provider).extract(task)


@pytest.mark.asyncio
async def test_publishing_requires_approval_policy():
    task = Task(
        "run",
        "publish",
        "token",
        1,
        StepSpec(id="publish", handler="guideline.publish"),
        {},
        {"draft": {}},
    )
    with pytest.raises(PermissionError):
        await GuidelineHandlers().publish(task)
