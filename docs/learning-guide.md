# 从 V0.1 读到 V1.0

建议按下面顺序读代码，每读完一层先运行对应测试，再改动实现。
中文模块注释解释职责与设计原因，避免只解释 Python 语法。

## 1. 定义、状态和行为分离

从 `domain/workflow/ids.py`、`definition.py`、`graph.py` 开始。
StepDefinition 描述某步骤是什么；StepRun 描述某一次执行的状态。
同一个定义可以执行多次，因此状态不能放进共享定义。

Graph 内部用可变集合建图，查询返回 tuple/frozenset，外部无法绕过接口添加依赖。
DFS 的 visiting 是当前路径，visited 是已检查的节点；共享父节点不等于成环。

`domain/execution/dependency_resolver.py` 只返回候选，不修改状态。
`state_machine.py` 只校验迁移，不执行副作用。先运行 `tests/unit/domain`。

## 2. 本地执行与异步

`application/orchestration/local_executor.py` 只有一次 handler 调用，异常原样抛出。
`scheduler.py` 管理状态、就绪计算和失败传播。异步版使用线程运行同步 handler。

注意：取消 await 不会强制停止线程。这正是持久版 worker 只接受 async handler，
不可信代码另放进 Docker 子进程的原因。超时需要 handler 进行协作式 await；
阻塞事件循环的 CPU 死循环不能靠 asyncio.timeout 安全终止。

## 3. 为什么增加 WorkflowSpec

Python callable、闭包和异常实例不能作为安全、稳定的数据库格式。
`domain/workflow/spec.py` 保存 handler 注册名、依赖、JSON 参数和执行策略。
Pydantic 在提交前验证名称、依赖存在、环、重试/超时范围。

Core `bootstrap.py` 创建通用运行设施；业务自己的 bootstrap 将注册名映射到函数。
API 不能提交任意模块路径或 Python 源码供进程直接导入。
工作流同名同版本不可覆盖；升级 handler 时也应使用新注册名或保持原语义。

## 4. 数据库本身就是队列

读 `infrastructure/persistence/models.py` 和 `store.py`，再运行
`tests/integration/test_durable_engine.py`。

一次步骤完成需要同时改变：步骤状态、输出检查点、子步骤就绪状态、事件、制品。
把这些写入同一个事务，避免“已成功但未通知下一步”的半完成状态。
steps 表既是运行状态也是队列，不必额外引入 Redis 与双写补偿。

生产写事务先锁 run，再修改步骤。领取使用 `FOR UPDATE SKIP LOCKED`，
遇到其他 worker 已锁住的 run 就跳过。锁只覆盖领取/提交，绝不覆盖模型调用。
同一 run 内的事务短暂串行，步骤执行本身仍可并行。

## 5. 阅读 worker 的生命周期

`Worker.tick()`：领取 Task → 启动 handler 和心跳 → 限时执行 → complete/fail。

每次领取产生新的 token。成功、失败和续租都要求 token 匹配且租约未过期。
worker 崩溃不会让任务永远停在 running；其他 worker 重新领取时恢复过期租约。
旧 worker 的晚到结果返回 False，不能覆盖新检查点。

`Task.idempotency_key` 在重试间保持不变，适合传递给支持去重的外部服务。
这不意味着引擎能保证外部副作用 exactly-once，详见 durability.md。

## 6. Agent 与工具边界

`application/agents/runner.py` 是有界模型循环：模型提议工具 → 运行时校验 →
工具结果作为数据回传 → 最终文本。最大轮数、工具调用数和上下文大小均受限。

`application/tools/runtime.py` 校验允许列表、JSON Schema、审批和超时。
模型输出不拥有授权能力；文档中的“请执行命令”也不改变工具权限。
DeepSeek 适配保留完整 assistant 消息，避免丢失 provider 多轮交互所需字段。

MCP 通过官方 SDK 管理 stdio 会话，进程命令由部署配置指定。
DockerSandbox 用无网络、只读根目录、非 root、内存/CPU/PID 限制执行代码。
工具的真实隔离能力取决于部署配置，不等同于纯 Python 路径检查。

## 7. 人工审批是持久状态

需要审批的步骤进入 waiting，创建审批记录，然后释放 worker。
审批通过后恢复 ready，拒绝则失败并阻止下游。无需让协程保持运行等人点击。
审批记录和状态在同一事务里更新；取消运行后不能通过审批重新激活它。

`src/applications/living_guideline/handlers.py` 展示完整业务流程。extract 验证每条引用确实存在
于对应原文；draft 装配人可阅读的证据草稿；publish 由审批解锁，并将版本输出
交给数据库事务保存。业务 handler 不负责创建数据库连接或 HTTP 路由。

## 8. 接口、界面与测试

`interfaces/api/app.py` 做认证、输入校验和资源访问；CLI 与 API 复用组合根。
SSE 提供事件 ID，客户端可以从游标继续读取。网页代理不会把 token 放进 URL。
前端以 React state 保存 token，刷新即丢失；没有 localStorage 持久化。

依次运行 unit → integration → applications/living_guideline → chaos。重点阅读旧 token 拒收、审批重放、
真正进程崩溃和伪造引文测试，它们验证的是行为约束，而非“某方法被调用了几次”。

可尝试的学习练习：增加一个纯计算 async handler，为它注册新名称，创建一个
有两条独立分支的 WorkflowSpec，观察并发、重试与事件时间线，再故意使一条分支失败。

## 官方资料

- [SQLAlchemy SELECT 与行锁](https://docs.sqlalchemy.org/en/20/core/selectable.html)
- [FastAPI 测试](https://fastapi.tiangolo.com/tutorial/testing/)
- [MCP 客户端](https://modelcontextprotocol.io/docs/develop/build-client)
- [Next.js App Router 安装与结构](https://nextjs.org/docs/app/getting-started/installation)
- [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/)

新 Agent 只在 `src/applications/` 或 `examples/` 定义并注册，不修改 Core。
运行 `uv run python examples/hello_workflow/workflow.py` 查看本地 DAG 示例；
运行 `uv run python examples/simple_agent/main.py` 查看外部注册 Agent 的持久执行示例。
