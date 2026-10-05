"""将同步 handler 卸载到线程，避免阻塞事件循环。

取消 await 无法强制停止 Python 线程，因此该执行器不承担持久 worker 的
强超时/恢复职责；参见 worker.py 的协程契约和 DockerSandbox。
"""

import asyncio

from agentflow.domain.workflow.definition import StepDefinition


class AsyncExecutor:
    async def execute(self, step: StepDefinition) -> None:
        await asyncio.to_thread(step.handler)
