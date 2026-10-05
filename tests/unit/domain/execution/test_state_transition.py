import pytest

from agentflow.domain.execution.exception import InvalidStateTransitionError
from agentflow.domain.execution.state_machine import StepStateMachine
from agentflow.domain.execution.status import StepStatus


def test_step_can_transition_from_pending_to_ready():
    machine = StepStateMachine()

    result = machine.transition(
        StepStatus.PENDING,
        StepStatus.READY,
    )

    assert result == StepStatus.READY


def test_step_can_transition_from_running_to_succeeded():
    machine = StepStateMachine()

    result = machine.transition(
        StepStatus.RUNNING,
        StepStatus.SUCCEEDED,
    )

    assert result == StepStatus.SUCCEEDED


def test_step_cannot_transition_from_succeeded_to_ready():
    machine = StepStateMachine()

    with pytest.raises(InvalidStateTransitionError):
        machine.transition(
            StepStatus.SUCCEEDED,
            StepStatus.READY,
        )


def test_step_cannot_transition_from_ready_to_failed():
    machine = StepStateMachine()

    with pytest.raises(InvalidStateTransitionError):
        machine.transition(
            StepStatus.READY,
            StepStatus.FAILED,
        )
