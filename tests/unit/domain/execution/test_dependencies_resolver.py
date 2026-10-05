import pytest

from agentflow.domain.execution.dependency_resolver import DependencyResolver
from agentflow.domain.execution.run import StepRun
from agentflow.domain.execution.status import StepStatus
from agentflow.domain.workflow.definition import StepDefinition
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId, StepRunId


def test_resolver_returns_pending_root_step():
    graph = WorkflowGraph()
    step = StepDefinition(
        id=StepId("A"),
        name="step_a",
        handler=lambda: None,
    )
    graph.add_step(step)

    step_runs = {
        step.id: StepRun(
            id=StepRunId("run-a"),
            step_id=step.id,
            status=StepStatus.PENDING,
        )
    }

    resolver = DependencyResolver()

    result = resolver.find_ready_steps(graph, step_runs)

    assert result == {step.id}


def test_resolver_returns_dependents_when_dependency_succeeds():
    graph = WorkflowGraph()

    a = StepDefinition(StepId("A"), "step_a", lambda: None)
    b = StepDefinition(StepId("B"), "step_b", lambda: None)
    c = StepDefinition(StepId("C"), "step_c", lambda: None)

    for step in (a, b, c):
        graph.add_step(step)

    graph.add_dependency(
        step_id=b.id,
        depends_on=a.id,
    )
    graph.add_dependency(
        step_id=c.id,
        depends_on=a.id,
    )

    step_runs = {
        a.id: StepRun(
            StepRunId("run-a"),
            a.id,
            StepStatus.SUCCEEDED,
        ),
        b.id: StepRun(
            StepRunId("run-b"),
            b.id,
            StepStatus.PENDING,
        ),
        c.id: StepRun(
            StepRunId("run-c"),
            c.id,
            StepStatus.PENDING,
        ),
    }

    resolver = DependencyResolver()

    result = resolver.find_ready_steps(graph, step_runs)

    assert result == {b.id, c.id}


def test_resolver_does_not_return_step_when_dependency_is_not_succeeded():
    graph = WorkflowGraph()

    a = StepDefinition(StepId("A"), "step_a", lambda: None)
    b = StepDefinition(StepId("B"), "step_b", lambda: None)

    graph.add_step(a)
    graph.add_step(b)

    graph.add_dependency(
        step_id=b.id,
        depends_on=a.id,
    )

    step_runs = {
        a.id: StepRun(
            StepRunId("run-a"),
            a.id,
            StepStatus.RUNNING,
        ),
        b.id: StepRun(
            StepRunId("run-b"),
            b.id,
            StepStatus.PENDING,
        ),
    }

    resolver = DependencyResolver()

    result = resolver.find_ready_steps(graph, step_runs)

    assert result == set()


@pytest.mark.parametrize("parent_status", list(StepStatus))
def test_resolver_requires_all_parents_and_does_not_modify_runs(parent_status):
    graph = WorkflowGraph()
    for name in ["a", "b", "c"]:
        graph.add_step(StepDefinition(StepId(name), name, lambda: None))
    graph.add_dependency(StepId("c"), StepId("a"))
    graph.add_dependency(StepId("c"), StepId("b"))
    runs = {
        StepId("a"): StepRun(StepRunId("a"), StepId("a"), StepStatus.SUCCEEDED),
        StepId("b"): StepRun(StepRunId("b"), StepId("b"), parent_status),
        StepId("c"): StepRun(StepRunId("c"), StepId("c"), StepStatus.PENDING),
    }
    before = {step_id: run.status for step_id, run in runs.items()}
    ready = DependencyResolver().find_ready_steps(graph, runs)
    assert (StepId("c") in ready) == (parent_status == StepStatus.SUCCEEDED)
    assert {step_id: run.status for step_id, run in runs.items()} == before


def test_resolver_does_not_return_step_that_is_already_ready():
    graph = WorkflowGraph()

    step = StepDefinition(
        StepId("A"),
        "step_a",
        lambda: None,
    )
    graph.add_step(step)

    step_runs = {
        step.id: StepRun(
            StepRunId("run-a"),
            step.id,
            StepStatus.READY,
        )
    }

    resolver = DependencyResolver()

    result = resolver.find_ready_steps(graph, step_runs)

    assert result == set()
