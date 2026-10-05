"""最小执行器只调用一次 handler；异常交由调度器决定如何处理。"""

from agentflow.domain.workflow.definition import StepDefinition


class LocalExecutor:
    def execute(self, step: StepDefinition) -> None:
        step.handler()
