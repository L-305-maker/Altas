import asyncio
from threading import Barrier, Lock

import pytest

from agentflow.application.orchestration.scheduler import Scheduler
from agentflow.domain.execution.status import StepStatus, WorkflowStatus
from agentflow.domain.workflow.definition import StepDefinition, WorkflowDefinition
from agentflow.domain.workflow.exception import CycleDetectedError
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId, WorkflowId


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def execute(request):
    def run(graph):
        workflow = WorkflowDefinition(WorkflowId("workflow"), "workflow")
        if request.param:
            return asyncio.run(
                Scheduler().run_async(workflow, graph, max_concurrency=2)
            )
        return Scheduler().run(workflow, graph)

    return run


def test_diamond_waits_for_all_dependencies_and_runs_each_step_once(execute):
    graph = WorkflowGraph()
    calls = []

    def handler(name, parents):
        def run():
            assert all(parent in calls for parent in parents)
            calls.append(name)

        return run

    # Insert in reverse order to ensure graph insertion does not dictate dependencies.
    for name, parents in [("d", ["b", "c"]), ("c", ["a"]), ("b", ["a"]), ("a", [])]:
        graph.add_step(StepDefinition(StepId(name), name, handler(name, parents)))
    for child, parent in [("d", "b"), ("d", "c"), ("b", "a"), ("c", "a")]:
        graph.add_dependency(StepId(child), StepId(parent))

    result = execute(graph)
    assert result.status == WorkflowStatus.SUCCEEDED
    assert sorted(calls) == ["a", "b", "c", "d"]
    assert all(
        step.status == StepStatus.SUCCEEDED for step in result.step_runs.values()
    )


def test_failure_cancels_descendants_but_runs_independent_branch(execute):
    graph = WorkflowGraph()
    calls = []
    error = RuntimeError("boom")

    def fail():
        raise error

    for name in ["grandchild", "child", "root", "independent"]:
        graph.add_step(
            StepDefinition(
                StepId(name),
                name,
                fail if name == "root" else lambda name=name: calls.append(name),
            )
        )
    graph.add_dependency(StepId("child"), StepId("root"))
    graph.add_dependency(StepId("grandchild"), StepId("child"))
    result = execute(graph)
    assert result.status == WorkflowStatus.FAILED
    assert result.step_runs[StepId("root")].error is error
    assert result.step_runs[StepId("root")].status == StepStatus.FAILED
    assert result.step_runs[StepId("child")].status == StepStatus.CANCELLED
    assert result.step_runs[StepId("grandchild")].status == StepStatus.CANCELLED
    assert calls == ["independent"]


def test_cycle_is_rejected_before_any_handler_runs(execute):
    graph = WorkflowGraph()
    calls = []
    for name in ["a", "b"]:
        graph.add_step(StepDefinition(StepId(name), name, lambda: calls.append(True)))
    graph.add_dependency(StepId("a"), StepId("b"))
    graph.add_dependency(StepId("b"), StepId("a"))
    with pytest.raises(CycleDetectedError):
        execute(graph)
    assert calls == []


def test_empty_graph_succeeds(execute):
    result = execute(WorkflowGraph())
    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.step_runs == {}


def test_repeated_runs_have_independent_states_and_ids(execute):
    graph = WorkflowGraph()
    graph.add_step(StepDefinition(StepId("a"), "a", lambda: None))
    first, second = execute(graph), execute(graph)
    assert first.id != second.id
    assert first.step_runs[StepId("a")].id != second.step_runs[StepId("a")].id
    assert first.step_runs[StepId("a")] is not second.step_runs[StepId("a")]


def test_async_scheduler_runs_concurrently_with_a_limit():
    graph = WorkflowGraph()
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    active = peak = 0

    def handler():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
        finally:
            with lock:
                active -= 1

    for name in ["a", "b", "c", "d"]:
        graph.add_step(StepDefinition(StepId(name), name, handler))
    result = asyncio.run(
        Scheduler().run_async(
            WorkflowDefinition(WorkflowId("w"), "w"),
            graph,
            max_concurrency=2,
        )
    )
    assert result.status == WorkflowStatus.SUCCEEDED
    assert peak == 2


@pytest.mark.parametrize("limit", [0, -1])
def test_async_scheduler_rejects_invalid_concurrency(limit):
    with pytest.raises(ValueError, match="max_concurrency"):
        asyncio.run(
            Scheduler().run_async(
                WorkflowDefinition(WorkflowId("w"), "w"),
                WorkflowGraph(),
                max_concurrency=limit,
            )
        )
