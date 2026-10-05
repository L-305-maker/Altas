import asyncio

from agentflow.domain.workflow.definition import StepDefinition


class AsyncExecutor:
    async def execute(self, step: StepDefinition) -> None:
        await asyncio.to_thread(step.handler)
