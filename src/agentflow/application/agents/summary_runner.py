"""Explicit independent summary execution; all model IO stays in DurableRunner."""

import asyncio

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.agents.summary import (
    SummarySourceError,
    SummaryValidationError,
    summary_messages,
    summary_tool,
    validate_summary_draft,
)
from agentflow.application.ports.agent_store import DurableSummaryStore
from agentflow.application.ports.durable_summary import TOOL_NAME
from agentflow.application.ports.handler_result import CommittedAgentResult
from agentflow.application.ports.model_provider import RequestModelProvider
from agentflow.application.ports.provider_admission import ProviderAdmissionStore
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.execution.task import Task


class DurableSummaryRunner:
    def __init__(
        self,
        store: DurableSummaryStore,
        provider: RequestModelProvider,
        *,
        admission: ProviderAdmissionStore | None = None,
    ):
        self.store, self.provider, self.admission = store, provider, admission

    async def run(self, task: Task) -> CommittedAgentResult:
        plan = await asyncio.to_thread(self.store.summary_source, task)

        async def read(_):
            current = await asyncio.to_thread(self.store.summary_source, task)
            if current != plan:
                raise SummarySourceError("SummarySourceChanged")
            return plan.tool_result()

        runtime = ToolRuntime()
        runtime.register(summary_tool(read))

        class Execution(DurableAgentRunner):
            def _initial_messages(self, task, prompt):
                return summary_messages(plan)

            def _proposals(self, state, response):
                calls = response.assistant_message.tool_calls
                if (
                    len(calls) != 1
                    or calls[0].function.name != TOOL_NAME
                    or state.actions
                ):
                    raise SummaryValidationError("SummaryToolNotAllowed")
                return super()._proposals(state, response)

            def _final_output(self, state, response):
                draft, _, _ = validate_summary_draft(
                    plan, state, response.assistant_message.content
                )
                return draft.model_dump(mode="json")

        return await Execution(
            self.store, self.provider, runtime, admission=self.admission
        ).run(task, "", allowed={TOOL_NAME})
