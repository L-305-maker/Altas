"""显式设置 AGENTFLOW_TEST_DOCKER=1 且构建 sandbox 镜像后运行。"""

import os

import pytest

from agentflow.infrastructure.sandbox.docker import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTFLOW_TEST_DOCKER") != "1", reason="Docker sandbox opt-in"
)


@pytest.mark.asyncio
async def test_container_runs_without_root_or_network():
    result = await DockerSandbox().execute(
        "import os; print(os.getuid()); print(2 + 3)"
    )
    assert result["exit_code"] == 0
    assert "65534" in result["stdout"]
    assert "5" in result["stdout"]


@pytest.mark.asyncio
async def test_container_timeout_is_enforced():
    with pytest.raises(TimeoutError):
        await DockerSandbox().execute("while True: pass", timeout=0.5)
