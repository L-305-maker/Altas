"""通用 runtime 组合：模型、工具和 Agent handler，不注册业务内容。"""

from pathlib import Path

from agentflow.application.agents.runner import AgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.config import Settings
from agentflow.infrastructure.models.deepseek import DeepSeekProvider
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.sandbox.docker import DockerSandbox
from agentflow.infrastructure.tools.filesystem import read_text_tool


def build_provider(settings: Settings):
    provider = None
    if settings.provider == "deepseek":
        key = (
            settings.deepseek_api_key.get_secret_value()
            if settings.deepseek_api_key
            else ""
        )
        provider = DeepSeekProvider(key, settings.model, settings.model_base_url)
    return provider


def build_handlers(
    settings: Settings, tools: ToolRuntime | None = None
) -> HandlerRegistry:
    registry = HandlerRegistry()
    tools = tools or ToolRuntime()
    provider = build_provider(settings)
    if settings.tool_root:
        tools.register(read_text_tool(Path(settings.tool_root)))
    if settings.sandbox_enabled:
        sandbox = DockerSandbox(settings.sandbox_image)

        async def python_tool(arguments):
            return await sandbox.execute(arguments["code"])

        tools.register(
            Tool(
                "python_sandbox",
                "在无网络容器内运行 Python",
                {
                    "type": "object",
                    "properties": {"code": {"type": "string", "maxLength": 100_000}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
                python_tool,
                requires_approval=True,
            )
        )

    async def agent(task):
        prompt = task.spec.parameters.get("prompt", task.inputs.get("prompt", ""))
        allowed = task.spec.parameters.get("tools", [])
        if (
            not isinstance(prompt, str)
            or not isinstance(allowed, list)
            or not all(isinstance(x, str) for x in allowed)
        ):
            raise ValueError("invalid agent prompt or tool allowlist")
        # MockProvider 每次新建，避免不同运行共享测试响应序列。
        runner = AgentRunner(provider or MockProvider(), tools)
        return await runner.run(
            prompt, allowed=set(allowed), approved=task.spec.requires_approval
        )

    registry.register("agent", agent)
    return registry
