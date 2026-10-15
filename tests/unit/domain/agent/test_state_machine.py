import pytest

from agentflow.domain.agent.status import AgentStatus, transition_agent
from agentflow.domain.execution.exception import InvalidStateTransitionError


@pytest.mark.parametrize(
    "path",
    [
        ["ready_model", "model_inflight", "succeeded"],
        [
            "ready_model",
            "model_inflight",
            "actions_pending",
            "waiting_approval",
            "actions_pending",
            "waiting_reconciliation",
            "actions_pending",
            "ready_model",
            "model_inflight",
            "succeeded",
        ],
        ["ready_model", "model_inflight", "ready_model"],
    ],
)
def test_normal_agent_paths(path):
    for current, target in zip(path, path[1:], strict=False):
        assert transition_agent(AgentStatus(current), AgentStatus(target)) == target


@pytest.mark.parametrize("current", [s for s in AgentStatus if not s.is_terminal])
@pytest.mark.parametrize(
    "target", [AgentStatus.FAILED, AgentStatus.EXHAUSTED, AgentStatus.CANCELLED]
)
def test_active_agent_can_stop(current, target):
    assert transition_agent(current, target) == target


@pytest.mark.parametrize("current", [s for s in AgentStatus if s.is_terminal])
@pytest.mark.parametrize("target", list(AgentStatus))
def test_terminal_agent_cannot_transition_even_to_itself(current, target):
    with pytest.raises(InvalidStateTransitionError):
        transition_agent(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentStatus.READY_MODEL, AgentStatus.SUCCEEDED),
        (AgentStatus.READY_MODEL, AgentStatus.ACTIONS_PENDING),
        (AgentStatus.MODEL_INFLIGHT, AgentStatus.WAITING_APPROVAL),
        (AgentStatus.ACTIONS_PENDING, AgentStatus.SUCCEEDED),
        (AgentStatus.WAITING_APPROVAL, AgentStatus.MODEL_INFLIGHT),
        (AgentStatus.WAITING_RECONCILIATION, AgentStatus.MODEL_INFLIGHT),
        (AgentStatus.READY_MODEL, AgentStatus.READY_MODEL),
    ],
)
def test_illegal_agent_transitions(current, target):
    with pytest.raises(InvalidStateTransitionError):
        transition_agent(current, target)
