import json
import sys

import httpx
import pytest
from jsonschema import ValidationError

from agentflow.application.agents.runner import AgentLimitError, AgentRunner
from agentflow.application.tools.runtime import Tool, ToolDeniedError, ToolRuntime
from agentflow.infrastructure.mcp.client import connect_tools
from agentflow.infrastructure.models.deepseek import DeepSeekProvider, ModelError
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.sandbox.docker import DockerSandbox
from agentflow.infrastructure.tools.filesystem import read_text_tool


@pytest.mark.asyncio
async def test_agent_executes_tool_then_returns_answer():
    runtime = ToolRuntime()

    async def add(arguments):
        return arguments["x"] + 1

    runtime.register(
        Tool(
            "add",
            "add",
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
                "additionalProperties": False,
            },
            add,
        )
    )
    provider = MockProvider(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"x": 2}'},
                    }
                ],
            },
            {"role": "assistant", "content": "3"},
        ]
    )
    assert await AgentRunner(provider, runtime).run("calculate", allowed={"add"}) == {
        "text": "3",
        "rounds": 2,
    }


@pytest.mark.asyncio
async def test_tool_permissions_approval_and_schema_are_enforced():
    runtime = ToolRuntime()
    called = []

    async def handler(arguments):
        called.append(arguments)

    runtime.register(
        Tool("write", "write", {"type": "object", "required": ["value"]}, handler, True)
    )
    with pytest.raises(ToolDeniedError):
        await runtime.call("write", {}, allowed=set())
    with pytest.raises(ToolDeniedError):
        await runtime.call("write", {}, allowed={"write"})
    with pytest.raises(ValidationError):
        await runtime.call("write", {}, allowed={"write"}, approved=True)
    assert called == []


@pytest.mark.asyncio
async def test_agent_round_limit():
    runtime = ToolRuntime()

    async def noop(arguments):
        return None

    runtime.register(Tool("noop", "noop", {"type": "object"}, noop))
    provider = MockProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "function": {"name": "noop", "arguments": "{}"}}
                ],
            }
        ]
    )
    with pytest.raises(AgentLimitError):
        await AgentRunner(provider, runtime, max_rounds=1).run("loop", allowed={"noop"})


@pytest.mark.asyncio
async def test_read_tool_rejects_parent_escape(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "private.txt").write_text("private")
    (root / "public.txt").write_text("public")
    tool = read_text_tool(root)
    assert await tool.invoke({"path": "public.txt"}) == {"text": "public"}
    with pytest.raises(ValueError):
        await tool.invoke({"path": "../private.txt"})


@pytest.mark.asyncio
async def test_deepseek_contract_without_network():
    def respond(request):
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "reasoning_content": "opaque",
                        }
                    }
                ]
            },
        )

    provider = DeepSeekProvider(
        "test-only", "test-model", transport=httpx.MockTransport(respond)
    )
    response = await provider.complete([{"role": "user", "content": "hello"}])
    assert response["reasoning_content"] == "opaque"


@pytest.mark.asyncio
async def test_provider_errors_do_not_contain_response_body():
    provider = DeepSeekProvider(
        "test-only",
        "test-model",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="sensitive response")
        ),
    )
    with pytest.raises(ModelError) as caught:
        await provider.complete([])
    assert "sensitive" not in str(caught.value)


def test_sandbox_command_has_no_host_mounts_or_network():
    command = DockerSandbox().command("test")
    for option in [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--memory=128m",
        "--pull=never",
    ]:
        assert option in command
    assert "-v" not in command


@pytest.mark.asyncio
async def test_real_mcp_stdio_roundtrip():
    runtime = ToolRuntime()
    async with connect_tools(
        sys.executable,
        ["examples/tool_agent/server.py"],
        runtime,
        allowed={"fingerprint"},
        requires_approval=False,
    ):
        response = await runtime.call(
            "fingerprint", {"text": "hello"}, allowed={"fingerprint"}
        )
    assert not response["isError"]
    assert "2cf24dba" in str(response)
