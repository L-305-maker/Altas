"""持久 worker：领取 → 心跳续租 → 限时执行 → 原子提交。

所有持久 handler 必须是 async，取消通过协程传播。无法终止的同步线程不适合
强超时语义，因此仅保留在 V0.1 LocalExecutor；不可信代码放到 Docker 子进程。
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from agentflow.application.ports.execution_store import ExecutionStore
from agentflow.domain.execution.task import Task

Handler = Callable[[Task], Awaitable[Any]]


class HandlerRegistry:
    def __init__(self):
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"duplicate handler: {name}")
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError("durable handlers must be async functions")
        self._handlers[name] = handler

    def get(self, name: str) -> Handler:
        try:
            return self._handlers[name]
        except KeyError:
            raise ValueError(f"unknown handler: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)


class Worker:
    def __init__(
        self,
        store: ExecutionStore,
        handlers: HandlerRegistry,
        *,
        lease_seconds: float = 30,
        concurrency: int = 4,
        poll_seconds: float = 0.5,
    ):
        if lease_seconds <= 0 or concurrency < 1 or poll_seconds <= 0:
            raise ValueError("invalid worker limits")
        self.store, self.handlers = store, handlers
        self.lease_seconds, self.concurrency, self.poll_seconds = (
            lease_seconds,
            concurrency,
            poll_seconds,
        )

    async def tick(self) -> bool:
        """执行至多一步；测试用这个入口无需启动永久循环。"""
        task = await asyncio.to_thread(self.store.claim, self.lease_seconds)
        if task is None:
            return False

        async def invoke():
            async with asyncio.timeout(task.spec.timeout):
                return await self.handlers.get(task.spec.handler)(task)

        job = asyncio.create_task(invoke())

        async def renew():
            try:
                while True:
                    await asyncio.sleep(self.lease_seconds / 3)
                    if not await asyncio.to_thread(
                        self.store.heartbeat, task, self.lease_seconds
                    ):
                        job.cancel()
                        return
            except Exception:  # noqa: BLE001 - DB failure means lease ownership is uncertain
                job.cancel()

        heartbeat = asyncio.create_task(renew())
        try:
            output = await job
            await asyncio.to_thread(self.store.complete, task, output)
        except asyncio.CancelledError:
            # 进程停止/租约丢失不提交失败；由租约过期恢复。外部取消继续向上传播。
            if asyncio.current_task().cancelling():
                raise
        except Exception as error:  # noqa: BLE001 - isolate user handlers at the worker boundary
            await asyncio.to_thread(self.store.fail, task, error)
        finally:
            job.cancel()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(asyncio.CancelledError, Exception):
                await job
        return True

    async def serve(self, stop: asyncio.Event) -> None:
        """固定槽位限制并发；每个槽位直接领取，避免先批量领取再排队导致租约过期。"""

        async def slot():
            while not stop.is_set():
                if not await self.tick():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.poll_seconds)

        async with asyncio.TaskGroup() as group:
            for _ in range(self.concurrency):
                group.create_task(slot())
