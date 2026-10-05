import asyncio
from uuid import uuid4

from agentflow.application.orchestration.async_executor import AsyncExecutor
from agentflow.application.orchestration.local_executor import LocalExecutor
from agentflow.domain.execution.dependency_resolver import DependencyResolver
from agentflow.domain.execution.run import StepRun, WorkflowRun
from agentflow.domain.execution.state_machine import (
    StepStateMachine,
    WorkflowStateMachine,
)
from agentflow.domain.execution.status import StepStatus, WorkflowStatus
from agentflow.domain.workflow.definition import WorkflowDefinition
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId, StepRunId, WorkflowRunId


class Scheduler:
    """Run an in-memory DAG; failed branches are cancelled, independent branches continue."""

    def run(self, workflow: WorkflowDefinition, graph: WorkflowGraph) -> WorkflowRun:
        run = self._start(workflow, graph)
        executor = LocalExecutor()
        while ready := self._ready(graph, run):
            for step_id in ready:
                step_run = run.step_runs[step_id]
                self._transition(step_run, StepStatus.RUNNING)
                try:
                    executor.execute(graph.get_step(step_id))
                except Exception as error:  # noqa: BLE001 - record user handler failures
                    step_run.error = error
                    self._transition(step_run, StepStatus.FAILED)
                else:
                    self._transition(step_run, StepStatus.SUCCEEDED)
        return self._finish(run)

    async def run_async(
        self,
        workflow: WorkflowDefinition,
        graph: WorkflowGraph,
        *,
        max_concurrency: int = 4,
    ) -> WorkflowRun:
        """Execute ready steps in bounded batches of synchronous handlers.

        Cancelling this coroutine cannot stop handlers already running in threads.
        """
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        run = self._start(workflow, graph)
        executor = AsyncExecutor()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def execute(step_id: StepId) -> None:
            async with semaphore:
                step_run = run.step_runs[step_id]
                self._transition(step_run, StepStatus.RUNNING)
                try:
                    await executor.execute(graph.get_step(step_id))
                except Exception as error:  # noqa: BLE001 - record user handler failures
                    step_run.error = error
                    self._transition(step_run, StepStatus.FAILED)
                else:
                    self._transition(step_run, StepStatus.SUCCEEDED)

        while ready := self._ready(graph, run):
            await asyncio.gather(*(execute(step_id) for step_id in ready))
        return self._finish(run)

    def _start(self, workflow: WorkflowDefinition, graph: WorkflowGraph) -> WorkflowRun:
        graph.validate()
        run = WorkflowRun(
            id=WorkflowRunId(str(uuid4())),
            workflow_id=workflow.id,
            status=WorkflowStatus.PENDING,
            step_runs={
                step_id: StepRun(
                    id=StepRunId(str(uuid4())),
                    step_id=step_id,
                    status=StepStatus.PENDING,
                )
                for step_id in graph.step_ids()
            },
        )
        run.status = WorkflowStateMachine().transition(
            run.status, WorkflowStatus.RUNNING
        )
        return run

    def _ready(self, graph: WorkflowGraph, run: WorkflowRun) -> tuple[StepId, ...]:
        # Repeat so failure propagates even when children were inserted first.
        changed = True
        while changed:
            changed = False
            for step_id, step_run in run.step_runs.items():
                if step_run.status == StepStatus.PENDING and any(
                    run.step_runs[parent].status
                    in {StepStatus.FAILED, StepStatus.CANCELLED}
                    for parent in graph.dependencies_of(step_id)
                ):
                    self._transition(step_run, StepStatus.CANCELLED)
                    changed = True

        candidates = DependencyResolver().find_ready_steps(graph, run.step_runs)
        ready = tuple(step_id for step_id in graph.step_ids() if step_id in candidates)
        for step_id in ready:
            self._transition(run.step_runs[step_id], StepStatus.READY)
        return ready

    def _transition(self, step_run: StepRun, target: StepStatus) -> None:
        step_run.status = StepStateMachine().transition(step_run.status, target)

    def _finish(self, run: WorkflowRun) -> WorkflowRun:
        status = (
            WorkflowStatus.SUCCEEDED
            if all(
                step.status == StepStatus.SUCCEEDED for step in run.step_runs.values()
            )
            else WorkflowStatus.FAILED
        )
        run.status = WorkflowStateMachine().transition(run.status, status)
        return run
