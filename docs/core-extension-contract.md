# Core extension contract 1.1

本文件定义外部 Consumer 可依赖的扩展入口和执行边界。1.1 是本契约的修订号；
本次不发布新的 distribution 版本，wheel / `agentflow.__version__` 仍为 1.0.0。
不新增数据库 Schema，Alembic Head 仍为 `0008`。

[Workflow Capacity Admission v1](workflow-capacity.md) 增加默认关闭的
`WorkflowAdmissionPolicy` / `WorkflowCapacityExceeded` 公共入口、受控 submit 和
`Store.queue_diagnostics()` / 认证队列聚合 API；不改变 Agent mode 或已有 Run 恢复。

## 能力与执行模式

| 能力 | 普通持久 Step | Legacy Agent（省略 mode） | 显式 Durable Agent |
| --- | --- | --- | --- |
| DAG、Run 快照、租约、取消、原子 Step 提交 | 有 | 有 | 有 |
| Handler 可能再次执行 | 是，at-least-once | 是，整步可能重跑 | 是，但内部已提交动作按持久状态恢复 |
| Agent Session、模型预约、Action、累积预算 | 无 | 无 | `SQLAgentStore` 持久保存 |
| 工具逐 Action 审批与审批消费 | 无；可用 Step 审批 | 仅历史 Step 审批语义 | 有；绑定动作、参数、工具策略和 revision |
| 派发结果不明时禁止自动重发 | Handler 自行处理 | 不提供此保证 | 有；模型 UNKNOWN 停止，工具 UNKNOWN 等待核对 |
| 已完成 Action 恢复时不重派 | 无此协议 | 无此协议 | 有 |
| 外部副作用物理 exactly-once | 不保证 | 不保证 | 不保证 |

`build_handlers(settings, tools=None, *, store=None)` 注册的 `agent` Handler
只在 `StepSpec.parameters.agent.mode == "durable_v1"` 时使用 Durable 路径。
省略 mode 保持 Legacy，即使传入了 Store；未知 mode 或 Durable 缺少 Store 会拒绝，
不会回退到 Legacy。`durable_summary` 是已有的显式 Durable Handler。
API 的 Handler 列表、版本号及普通持久 Worker 都不证明某 Handler 具备 Durable 能力。
自定义 Handler 必须自己验证配置并调用正确 Runner，mode 标签本身不执行这种验证。

新工作流应在定义中显式声明模式，并使用独立、带版本的 Handler 名称。没有全局
“默认 Durable”开关；本次不添加隐式能力升级，也不改写恢复中 Session 的模式。

## 公共 Python 入口

外部集成使用 `agentflow.runtime`。这是原有对象的别名 Facade，没有包装层或新默认值；
导入不建立数据库连接、不迁移、不启动 Worker、不调用模型。
稳定范围是下列对象及本文说明的用法，不是这些对象的全部私有属性。

| 用途 | 入口 |
| --- | --- |
| 定义与调度 | `WorkflowSpec`, `StepSpec`, `Task`, `HandlerRegistry`, `Worker` |
| 组合服务 | `Settings`, `create_app`, `build_handlers` |
| 持久化适配 | `ExecutionStore`, `AgentStore`, `Store`, `SQLAgentStore` |
| Agent | `AgentRunner`, `DurableAgentRunner`, `CommittedAgentResult` |
| 模型适配 | `ModelProvider`, `DurableModelProvider`, `RequestModelProvider`, `ModelResponse`, `MockProvider` |
| 请求预检 | `ModelRequestOptions`, `ProviderCapabilities`, `PreparedModelRequest`, `build_request` |
| 工具适配 | `Tool`, `ToolRuntime`, `ToolInvocation`, `ToolSideEffect` |
| 审核决定 | `ApprovalDecision`, `ReconciliationDecision` |
| 安全错误 | `ConflictError`, `NotFoundError`, `LostLease`, `StateConflict`, `AgentDataError`, `AgentStoreUnavailable`, `ModelConfigurationError`, `ModelProtocolError`, `ModelOutcomeUnknown`, `ModelRetryableError`, `ModelRateLimitedError`, `ModelRequestError`, `ToolDeniedError`, `ToolApprovalRequired`, `ToolConfirmedFailure` |

旧模块路径不删除，现有调用仍兼容。新增 Consumer 应使用 Facade，避免依赖目录布局。
新增公开接口应为显式可选项；改变既有默认值、字段含义、协议版本或恢复规则需要先做
兼容性设计，不能以同名 Handler 新实现静默升级存量 Run。

## 注册 Handler 与组合 API / Worker

```python
from agentflow.runtime import HandlerRegistry, StepSpec, WorkflowSpec

handlers = HandlerRegistry()

async def normalize(task):
    return {"text": task.inputs["text"].strip()}

handlers.register("example.normalize.v1", normalize)
spec = WorkflowSpec(
    name="example",
    version=1,
    steps=(StepSpec(id="normalize", handler="example.normalize.v1", artifact=True),),
)
```

Handler 必须是 async，接收一个 `Task`，返回可 JSON 序列化的结果。
`Task` 提供 `run_id`, `step_id`, `token`, `attempt`, `spec`, `inputs`, `dependencies`；
`idempotency_key` 是稳定的 `run_id:step_id`，跨重领不变，不能据此宣称外部系统已经去重。
Registry 的 `register/get/names` 是允许的注册接口；重复名称会拒绝。
API JSON 只选择已注册名称，不能指定任意 Python 导入路径。

API 进程组合 `create_app(settings, store, handlers)`，Worker 进程组合
`Worker(store, handlers, lease_seconds=..., concurrency=..., poll_seconds=...)`。
两者连接同一数据库，部署同一版本的 Handler / 工具 / Provider 配置。
`tick()` 执行至多一个可领取 Step；长期运行用 `serve(stop: asyncio.Event)`。
停止后等待在途任务结束，再关闭调用方拥有的资源。

`Store(url)` 的 `register(spec)`, `submit(workflow_id, inputs, key=...)`,
`get_run`, `artifacts`, `cancel`, `claim`, `heartbeat`, `complete`, `fail`,
`validate_agent_result`, `close` 可用于组合与执行适配；通常把领取和提交交给 Worker。
同一 submission key 和相同请求返回同一个 Run，冲突请求拒绝。
`create_schema()` 只用于新建、可丢弃的测试库；部署升级使用已审核的 Alembic 迁移。
注入 API 的 Store 由调用方关闭；API 自建的 Store 随应用生命周期关闭。

## 版本与快照

同名同版本 Workflow 定义不可覆盖；相同定义可以重复注册，定义变化必须递增版本。
提交保存 Workflow 定义和输入快照，恢复时 `Task.spec` 来自 Run 快照。
注册新版本、增加 Handler、改变新部署的配置默认值不会重写该快照。

快照不包含 Python 函数机器码、第三方服务行为或整个部署环境。用同一个 Handler 名称
部署不同代码仍可能改变执行行为，Consumer 必须保留旧版本 Handler 和匹配的配置。
Pydantic 的 frozen 不使嵌套 `parameters` 字典深度不可变；不要原地修改共享定义或 Task。
Tool 版本 / 策略摘要和模型请求身份可以检测其覆盖范围内的漂移，不能证明代码未变。

## 模型接口与生命周期

Legacy `ModelProvider.complete(messages, tools=None)` 返回 assistant 消息字典。
历史 `DurableModelProvider.respond(...)` 返回校验过的 `ModelResponse`，但仅实现
`respond()` 已不足以进入当前 Durable Runner。

当前 Durable 扩展必须实现 `RequestModelProvider.prepare_request(messages, tools,
projection) -> PreparedModelRequest`。准备阶段是无派发的预检：用 `build_request`
冻结消息、工具 Schema、选项、能力、适配器版本和上下文投影身份；返回 request 和
异步 `execute` 闭包。闭包只在预约、准入和数据库提交成功后由 Runner 调用。
构建 `ProviderCapabilities` 时可用字段字典声明 `provider_key`；不能用模型输出
冒充可信能力或 token 计数。`MockProvider.prepare_request` 是离线最小实现示例。
只有 `respond()` 的适配器在派发前以 `ModelRequestContractUnavailable` 拒绝。

保留 ModelRequestContract v1/v2、Checkpoint v1–v3、Summary / Consumption、
Provider Admission、Usage 与 Token Preflight 的既有读取和恢复规则。
不补造旧记录的请求身份，不用新的配置默认值重解释旧 Session。
版本化序列化对象是持久协议；不承诺其所有内部 Python 构造器为通用插件入口。
高级计数器、Summary 和准入策略的新增插件接口应另行明确契约。

Consumer 拥有自己创建的 Provider、HTTP 客户端和工具连接，负责启动和关闭；
Runner 不假设 Provider 存在 `close()`，也不会替调用方关闭这些资源。
Provider 必须准确区分“确认未完成，可重试”和“已派发但结果不明”。
`ModelRetryableError` / `ModelRateLimitedError` 只用于前者；后者使用
`ModelOutcomeUnknown`。未知结果不能转换成可重试异常。

## 工具、审批与恢复

通过 `ToolRuntime.register(Tool(...))` 注册名称、说明、JSON Schema、async `invoke`、
`requires_approval`、正数 timeout、`ToolSideEffect` 和 version。
Durable 工具建议实现 `invoke_durable(arguments, ToolInvocation)`，将 action ID 和
稳定 idempotency key 交给支持去重的外部服务；若省略，现有协议允许调用 `invoke`，
但工具将收不到该 key。`allowed` 白名单、参数校验和工具策略绑定均不能跳过。
`UNSAFE`, `READ_ONLY`, `IDEMPOTENT`, `RECONCILABLE` 是策略声明，不是重发 UNKNOWN 的许可。

Durable Runner 必须搭配完整 `AgentStore` 协议，默认实现是
`SQLAgentStore(store)`。它必须共享 ExecutionStore 的事务、数据库时间、租约 token、
CAS revision、取消检查及原子最终提交；普通键值缓存不能替代它。
外部实现必须通过现有崩溃、提交丢失、审批、UNKNOWN 和取消契约测试。
自定义 Durable Handler 使用
`await DurableAgentRunner(agents, provider, tools).run(task, goal, allowed={...})`，
直接返回其提交结果给 Worker。Legacy 对应 `AgentRunner(provider, tools).run(...)`；
选择 Runner 是 Handler 的责任，不能根据一个返回类型追认更强的运行保证。

流程是：持久预约 → 批准（受保护动作）→ 消费与绑定批准 → 标记派发 → 调用外部工具
→ 持久确认。Step 审批不授权任意未来 Action；Legacy 的 `approved` 布尔值不能作为
Durable 的逐动作批准凭据。可通过认证的 Action Approval API 查询并以 expected revision
作出决定；核对使用 Reconciliation API，记录证据引用和操作者。
`SQLAgentStore` 的 `get_action_approval`, `list_pending_action_approvals`,
`review_history`, `list_unknown_actions` 可用于审核查询；状态变化应经协议方法或 API，
不能写 ORM 行“修复”状态。

退出后由新 Worker 从 PostgreSQL 恢复。已完成动作不重派；INFLIGHT 确认丢失转 UNKNOWN。
模型 UNKNOWN 停止，工具 UNKNOWN 等待人工核对，不自动重试，包括标记幂等的工具。
明确的核对决定经 CAS 应用；它不使物理外部执行自动变成 exactly-once。

## 错误、超时、取消与提交

错误采用安全类型 / 受约束错误码。`ToolConfirmedFailure(code)` 仅用于工具能确认的失败，
不得包含原始响应、凭据、业务正文或上游异常消息。模型和存储错误也不得夹带这些内容。
普通 Worker 只持久记录异常类型；它可能按 Step 策略重试整个 Handler。
`LostLease`, `StateConflict`, `AgentStoreUnavailable` 表示应停止当前持有者，
不能包装成普通异常来触发整步重跑。API 的冲突、缺失、认证和不可用响应保留现有安全格式。

Step timeout、工具 timeout 和 Worker 心跳取消是协作式 async 取消，不保证终止任意
同步线程或撤销已经发生的外部副作用。不要吞掉 `CancelledError`；不可信同步代码
应置于已有隔离执行机制中。取消 / 旧 Lease 后的提交必须由 Store 拒绝，数据库时间
决定所有权，客户端时钟不能延长租约。

普通输出由 Worker 调用 `complete`。Durable Runner 在同一事务内提交 Session、最终
Step 状态、事件和 Artifact，再返回 `CommittedAgentResult`；Worker 验证所属 Task 和
真实持久状态，不重复 complete。伪造这个返回值不能提交成功，也不是外部副作用证明。
Artifact digest 是现有 JSON 编码（`ensure_ascii=False, sort_keys=True, allow_nan=False`，
默认分隔符）UTF-8 字节的 SHA-256；不要与其他协议的紧凑 canonical JSON 摘要混淆。
这些保证限定在 Core 数据库事务内。外部 exactly-once 还需要外部幂等、查询 / 核对和
故障协议及独立证据。

## 独立 Consumer 的安装与验证

`examples/independent_consumer/` 是纯 Mock 外部业务夹具，只从 Facade 导入 Core，
不导入 `applications`、ORM、仓库测试或脚本。它注册自己的 Handler，运行
prepare → durable agent → audit DAG，并生成两份摘要可核验的 Artifact。
它不是 Living Guideline 的另一个入口，也不增加 Core 对业务的反向依赖。

```powershell
uv build
uv run python scripts/verify_independent_consumer.py --wheel dist/agentflow-1.0.0-py3-none-any.whl --bin D:/PSQL/bin
```

Linux 的 `--bin` 可传 `pg_config --bindir` 的结果。脚本只创建自有的临时 loopback
PostgreSQL 和随机测试 token，没有连接既有数据库的参数。它在仓库之外创建新 venv，
安装 wheel，把 Consumer 复制出去，以 `python -I` 导入和启动，并验证 Core 确实来自
该 venv 的 site-packages。迁移使用仓库审核过的脚本，但关闭源码路径注入，迁移导入
的 Core 也来自已安装 wheel。不会把测试凭据或数据库 URL 输出到日志。

验证调用真实认证 ASGI API（TestClient，无独立 TCP 服务），启动多个真实 OS Worker
子进程，并在真实 PostgreSQL 上检查：注册 / 提交幂等与冲突、Run 快照、模拟操作者
批准后的跨进程恢复、已完成 Action 不重派、外部 Mock 效果之后进程退出造成的 UNKNOWN、
重启不重发、显式核对恢复、真实数据库时间租约过期后的旧持有者拒绝、取消后迟到写入
拒绝，以及待 Action 审批的 Durable Session 取消后不派发。进程退出只作用于自有夹具。

## 内部边界与后续工作

ORM 表、SQL / 锁实现、私有事务方法、Checkpoint 编解码器、上下文投影内部算法、
Worker 私有任务管理、CLI 装配细节和具体业务应用均不属于稳定公共 Python 扩展 API。
不调用 `Store.engine`、`transaction` 或私有属性实现 Consumer 的正常业务。
公开 Store 协议不授权绕过原子提交、CAS、Fencing、取消或 UNKNOWN 规则。

本契约不增加 Planner、RAG、长期 Memory、Multi-Agent 或 Multi-Tenant。
现有预算、准入和观测用量为后续资源治理提供基础，但不是费用结算保证；当前 Runner
不提供完整定价 / 资源单位计费协议。应先定义计量来源、预约与结算、UNKNOWN 预留和
升级兼容，再扩展资源治理。
真实双宿主、真实 Provider / Token Conformance、HA / PITR 和正式生产发布继续
`NOT_VERIFIED`。本轮离线软件测试通过不构成生产 RC 验收。
