from dataclasses import FrozenInstanceError
import pytest

from agentflow.domain.workflow.ids import StepId, WorkflowId, StepRunId, WorkflowRunId


@pytest.mark.parametrize(
        "id_type",
        [StepRunId,StepId,WorkflowRunId,WorkflowId]
)
def test_value_equal(id_type):
    a = id_type("123")
    b = id_type("123")

    assert a==b


def test_workflow_id_and_step_id_with_same_value_are_not_equal():
    workflow_id = WorkflowId("123456")
    step_id = StepId("123456")

    assert workflow_id != step_id


@pytest.mark.parametrize(
        "id_type",
        [StepId,StepRunId,WorkflowId,WorkflowRunId]
)
def test_workflow_id_value_cannot_be_changed(id_type):
    _id = id_type("123456")

    with pytest.raises(FrozenInstanceError):
        _id.value = "654321"