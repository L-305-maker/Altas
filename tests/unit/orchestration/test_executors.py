import asyncio
from unittest.mock import Mock

import pytest

from agentflow.application.orchestration.async_executor import AsyncExecutor
from agentflow.application.orchestration.local_executor import LocalExecutor
from agentflow.domain.workflow.definition import StepDefinition
from agentflow.domain.workflow.ids import StepId


@pytest.mark.parametrize("asynchronous", [False, True])
def test_executor_calls_handler_once(asynchronous):
    handler = Mock(return_value=None)
    step = StepDefinition(StepId("a"), "a", handler)
    if asynchronous:
        result = asyncio.run(AsyncExecutor().execute(step))
    else:
        result = LocalExecutor().execute(step)
    assert result is None
    handler.assert_called_once_with()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_executor_propagates_original_exception(asynchronous):
    error = RuntimeError("boom")
    handler = Mock(side_effect=error)
    step = StepDefinition(StepId("a"), "a", handler)
    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(AsyncExecutor().execute(step))
        else:
            LocalExecutor().execute(step)
    assert caught.value is error
    handler.assert_called_once_with()
