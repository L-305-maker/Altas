import pytest

from agentflow.domain.workflow.definition import StepDefinition
from agentflow.domain.workflow.exception import (
    CycleDetectedError,
    DuplicateStepError,
    SelfDependencyError,
    StepNotFoundError,
)
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId


def test_graph_can_add_step():
    graph = WorkflowGraph()
    step = StepDefinition(
        id=StepId("A"),
        name="step_a",
        handler=lambda: None,
    )

    graph.add_step(step)


def test_graph_rejects_duplicate_step():
    graph = WorkflowGraph()
    step = StepDefinition(
        id=StepId("A"),
        name="step_a",
        handler=lambda: None,
    )
    graph.add_step(step)

    with pytest.raises(DuplicateStepError):
        graph.add_step(step)


def test_graph_rejects_self_dependency():
    graph = WorkflowGraph()
    step = StepDefinition(
        id=StepId("A"),
        name="step_a",
        handler=lambda: None,
    )
    graph.add_step(step)

    with pytest.raises(SelfDependencyError):
        graph.add_dependency(
            step_id=step.id,
            depends_on=step.id,
        )


def test_graph_rejects_missing_dependency():
    graph = WorkflowGraph()
    step = StepDefinition(
        id=StepId("B"),
        name="step_b",
        handler=lambda: None,
    )
    graph.add_step(step)

    with pytest.raises(StepNotFoundError):
        graph.add_dependency(
            step_id=step.id,
            depends_on=StepId("A"),
        )


def test_graph_accepts_acyclic_dependencies():
    graph = WorkflowGraph()
    a = StepDefinition(StepId("123"), "a", lambda: None)
    b = StepDefinition(StepId("234"), "b", lambda: None)
    c = StepDefinition(StepId("678"), "c", lambda: None)

    for e in {a, b, c}:
        graph.add_step(e)
    graph.add_dependency(a.id, b.id)
    graph.add_dependency(b.id, c.id)

    assert graph.validate() is None


def test_graph_rejects_cycle():
    graph = WorkflowGraph()
    a = StepDefinition(StepId("123"), "a", lambda: None)
    b = StepDefinition(StepId("234"), "b", lambda: None)
    c = StepDefinition(StepId("678"), "c", lambda: None)

    for e in (a, b, c):
        graph.add_step(e)
    graph.add_dependency(a.id, b.id)
    graph.add_dependency(b.id, c.id)
    graph.add_dependency(c.id, a.id)

    with pytest.raises(CycleDetectedError):
        graph.validate()


def test_graph_queries_return_immutable_snapshots():
    graph = WorkflowGraph()
    a = StepDefinition(StepId("a"), "a", lambda: None)
    b = StepDefinition(StepId("b"), "b", lambda: None)
    graph.add_step(a)
    ids = graph.step_ids()
    dependencies = graph.dependencies_of(a.id)
    graph.add_step(b)
    graph.add_dependency(a.id, b.id)

    assert ids == (a.id,)
    assert dependencies == frozenset()
    assert isinstance(dependencies, frozenset)
    assert graph.dependencies_of(a.id) == frozenset({b.id})
    assert graph.get_step(a.id) is a


def test_graph_validates_disconnected_components():
    graph = WorkflowGraph()
    for name in ["root", "a", "b"]:
        graph.add_step(StepDefinition(StepId(name), name, lambda: None))
    graph.add_dependency(StepId("a"), StepId("b"))
    graph.add_dependency(StepId("b"), StepId("a"))
    with pytest.raises(CycleDetectedError):
        graph.validate()
