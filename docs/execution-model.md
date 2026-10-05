# V0.1 Local Engine

本文仅描述保留的本地教学入口。V1.0 服务模式使用 `Worker + Store`，其持久化、
恢复、审批和取消语义见 [durability.md](durability.md)，启动方式见仓库 README。

`WorkflowGraph` 保存步骤定义和依赖，`DependencyResolver` 只查询就绪候选，
`Scheduler` 协调运行状态与执行器。Application 依赖 Domain。

## 使用

运行 `uv run python examples/hello_workflow/workflow.py` 查看菱形 DAG 的串行与并发执行。

- `Scheduler().run(workflow, graph)`：同步串行执行，返回 `WorkflowRun`。
- `await Scheduler().run_async(workflow, graph, max_concurrency=4)`：异步入口，限制同时执行的步骤数。
- `WorkflowRun.step_runs`：按 `StepId` 查询本次运行状态；失败步骤的 `error` 保留原始异常。

每次调用都会验证 DAG，并创建独立的 WorkflowRunId、StepRunId 和运行状态。
图应在调用前构建完成，执行期间不要修改它。空图成功结束，环在任何 handler 执行前被拒绝。

## 步骤与状态

`StepDefinition.handler` 的契约是 `Callable[[], None]`：无参数、同步、无业务返回值。
不要传入 `async def`；AsyncExecutor 使用 `asyncio.to_thread` 执行同步 handler。
LocalExecutor 和 AsyncExecutor 都只调用一次 handler，异常原样向上传播。

调度器负责 `PENDING → READY → RUNNING → SUCCEEDED / FAILED`。
只有全部依赖成功，步骤才就绪。失败步骤的下游递归进入 `CANCELLED`，
独立分支继续执行。调度器捕获普通 handler 异常并保存在 StepRun 中，
工作流最终返回 `FAILED`；全部步骤成功则返回 `SUCCEEDED`。

异步模式按就绪批次执行，一批结束后再解锁下一批。并发步骤应自行保证共享数据的线程安全。
取消异步调用不能强制停止已经运行的 Python 线程；本版不提供运行中取消、超时、重试、
恢复、输入输出传递或持久化。线程卸载主要适用于阻塞 I/O，不承诺 CPU 密集型任务加速。

## 参考

参考 [Dask local scheduler](https://github.com/dask/dask/blob/main/dask/local.py)
对 dependencies、ready、running、finished 的区分，保留图定义与运行状态分离的设计。
本实现沿用项目已有领域模型，未引入 Dask 依赖或复制其调度代码。

验证：`uv run pytest -q`。覆盖单步执行、异常身份、菱形依赖、环拒绝、失败传播、
并发限制、空图与多次运行隔离。
