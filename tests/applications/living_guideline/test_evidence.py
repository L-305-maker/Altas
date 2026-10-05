import json

import pytest

from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import StepSpec
from agentflow.infrastructure.models.mock import MockProvider
from applications.living_guideline.handlers import GuidelineHandlers


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
