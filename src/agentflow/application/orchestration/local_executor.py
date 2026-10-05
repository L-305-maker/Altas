from agentflow.domain.workflow.definition import StepDefinition


class LocalExecutor:
    def execute(self, step: StepDefinition) -> None:
        step.handler()
