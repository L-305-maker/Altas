import asyncio

from agentflow.application.orchestration.scheduler import Scheduler
from agentflow.domain.workflow.definition import StepDefinition, WorkflowDefinition
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId, WorkflowId


def main() -> None:
    workflow = WorkflowDefinition(WorkflowId("hello"), "Hello workflow")
    graph = WorkflowGraph()
    for name in ["start", "left", "right", "finish"]:
        graph.add_step(
            StepDefinition(
                StepId(name),
                name,
                lambda name=name: print(f"Executing {name}"),
            )
        )
    for child, parent in [
        ("left", "start"),
        ("right", "start"),
        ("finish", "left"),
        ("finish", "right"),
    ]:
        graph.add_dependency(StepId(child), StepId(parent))

    scheduler = Scheduler()
    print("Serial:", scheduler.run(workflow, graph).status)
    result = asyncio.run(scheduler.run_async(workflow, graph, max_concurrency=2))
    print("Concurrent:", result.status)
    for step_id, step_run in result.step_runs.items():
        print(step_id.value, step_run.status)


if __name__ == "__main__":
    main()
