from agentflow.domain.execution.run import StepRun
from agentflow.domain.execution.status import StepStatus
from agentflow.domain.workflow.graph import WorkflowGraph
from agentflow.domain.workflow.ids import StepId


class DependencyResolver:
    def find_ready_steps(self,graph: WorkflowGraph,step_runs: dict[StepId, StepRun],) -> set[StepId]:
        ready_steps: set[StepId] = set()

        for step_id in graph.step_ids():
            step_run = step_runs[step_id]

            # 只有 PENDING Step 才是 READY 候选
            if step_run.status != StepStatus.PENDING:
                continue

            dependencies = graph.dependencies_of(step_id)

            # 所有依赖都成功，当前 Step 才能执行
            all_dependencies_succeeded = all(step_runs[dependency_id].status == StepStatus.SUCCEEDED for dependency_id in dependencies)

            if all_dependencies_succeeded:
                ready_steps.add(step_id)

        return ready_steps