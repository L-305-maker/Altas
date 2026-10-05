"""可变的建图接口与只读查询接口分离，避免调用者绕过校验修改依赖集合。"""

from agentflow.domain.workflow.definition import StepDefinition
from agentflow.domain.workflow.exception import (
    CycleDetectedError,
    DuplicateStepError,
    SelfDependencyError,
    StepNotFoundError,
)
from agentflow.domain.workflow.ids import StepId


class WorkflowGraph:
    def __init__(self) -> None:
        self._steps: dict[StepId, StepDefinition] = {}
        self._dependencies: dict[StepId, set[StepId]] = {}

    def add_step(self, step: StepDefinition):

        if step.id in self._steps:
            raise DuplicateStepError("depulicate step")
        self._steps[step.id] = step
        self._dependencies[step.id] = set()

    def add_dependency(self, step_id: StepId, depends_on: StepId):

        if step_id not in self._steps or depends_on not in self._steps:
            raise StepNotFoundError("Not find the step")
        if step_id == depends_on:
            raise SelfDependencyError("the step depends on itself")

        self._dependencies[step_id].add(depends_on)

    def validate(self) -> None:
        """DFS 中 visiting 表示当前递归路径，visited 表示已完整检查的节点。

        再次遇到 visiting 才是环；遇到 visited 是合法的共享依赖（例如菱形 DAG）。
        """
        visited = set()
        visiting = set()

        def dfs(u) -> bool:
            if u in visiting:
                return True
            if u in visited:
                return False
            visiting.add(u)
            for v in self._dependencies[u]:
                if dfs(v):
                    return True
            visiting.remove(u)
            visited.add(u)
            return False

        for step in self._steps:
            if dfs(step):
                raise CycleDetectedError()

    def step_ids(self) -> tuple[StepId, ...]:
        return tuple(self._steps)

    def get_step(self, step_id: StepId) -> StepDefinition:
        return self._steps[step_id]

    def dependencies_of(self, stepid: StepId) -> frozenset[StepId]:
        return frozenset(self._dependencies[stepid])
