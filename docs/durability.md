# 持久执行语义

## 事务与恢复

注册的工作流版本不可变，运行保存定义快照、输入、独立步骤状态和输出检查点。
成功步骤不会在正常恢复时重新执行。未成功步骤通过数据库队列重新领取。

领取事务将 ready 改为 running，同时增加 attempts、生成 token 并设置 lease_until。
worker 每隔租约长度的三分之一续租。PostgreSQL 使用数据库时钟判断租约，
避免多个 worker 主机时钟不同步。SQLite 使用本机时间，仅用于单机学习模式。

超时、普通异常、租约过期都消耗一次尝试。未超过 max_attempts 时延迟重试，
退避为 retry_delay × 2^(attempts-1)，上限 3600 秒；耗尽后失败并取消下游。
独立分支仍能完成。工作流只有所有步骤都成功才为 succeeded。

## 至少一次，不是副作用恰好一次

如果工具已发送外部请求，但进程在写成功检查点前崩溃，引擎无法知道外部请求是否完成。
恢复时可能再次执行该步骤。这是 at-least-once 的必然边界。

有副作用的 handler 应使用稳定的 `Task.idempotency_key`，或将外部操作与业务
去重记录放在同一个受控事务中。新 token 只能防止旧执行覆盖检查点，不能撤销
已经发送的邮件、支付或文件写入。

运行取消会使租约失效，协作式 handler 在下次心跳发现后取消；已经发生的外部
副作用不会回滚。用户编写的 handler 必须传播 CancelledError，不应阻塞事件循环。
DockerSandbox 取消时会删除对应容器。

## 审批与版本

审批不是暂存在内存中的 Future，而是数据库资源。审批通过/拒绝具有终局性，
同一决定的重放不会再次执行状态迁移；相反决定产生冲突。已取消运行不能恢复审批。
每个成功的 artifact 步骤只产生一个制品，输出和成功状态原子提交。

运行 ID 同时作为业务修订标识；制品包含内容 SHA-256。数据库中不提供覆盖版本的 API。
本版没有跨工作流的语义 diff 或自动合并，后续修订应创建新运行。

## 容量边界

每个工作流最多 500 步，每个 JSON 输入/输出最多 2 MB。
本版在事务中扫描单个 run 的步骤，适合学习和中小规模工作流；大量超宽 DAG
应通过基准分析后再做增量依赖计数。没有实现跨地区共识、自动分片或分布式事务。

## Durable AgentStore（PR2）

先运行 Alembic `upgrade head`，再部署本版代码；迁移 `0002` 仅新增四张 Agent 表，
不会改变 `0001` 的 Workflow/Step 数据。回退至 `0001` 会删除 Agent 数据，旧工作流数据保留。
本轮不让现有 AgentRunner 启用持久执行，也不提供等待步骤的通用恢复入口。

应用依赖 `application.ports.agent_store.AgentStore`；实现为
`SQLAgentStore(existing_store)`，复用同一 Engine、事务和数据库时钟。
Task 的 `step_id` 是步骤 key，Store 先按 `(run_id, key)` 找到真实 StepRow UUID，
Session 的唯一外键绑定该 UUID。锁顺序为 Run → Step → AgentSession → 模型记录 → Action。
同一 Run 的事务会串行化，这是当前可靠性边界，尚未针对超宽并行 Agent 做吞吐优化。

- `open(task, initial_snapshot, messages=...)`：只接受未使用的 READY_MODEL 快照；
  同一 Step、相同初始化幂等返回现状，冲突初始化不会覆盖历史。
- `load(task)`：校验当前租约后恢复快照、全部有序消息、全部逻辑模型调用和历史 Action。
- `commit_transition(task, AgentTransition(...))`：校验旧快照和修订号，调用 `old.advance(candidate)`，
  再用独立 revision 列执行 CAS；消息、模型调用、Action 和快照在同一事务写入。
- `complete_agent_step(task, transition, output)`：SUCCEEDED、Step 输出、Artifact 及下游调度原子提交。
- `fail_agent_step(task, transition)`：FAILED/EXHAUSTED 与 Step failed 原子提交，不重开终态 Agent。
- `suspend_agent_step(task, transition)`：等待快照与 Step waiting 原子提交并释放 token/租约。
  现有 Workflow 审批入口不能任意恢复 Agent 等待步骤；授权恢复由后续 PR 设计。

每次模型请求先提交 READY_MODEL → MODEL_INFLIGHT，逻辑调用 ID 与轮次固定；
确认失败的重试复用 ID 并使 attempts 恰好加一。未知模型结果不能直接预约重试。
成功模型结果必须附带一条 assistant 消息，其 `payload.action_ids` 与有序批次完全一致。
ActionArguments 必须逐个匹配新批次；规范 JSON SHA-256 必须与领域摘要相符。
`arguments_digest` 用于一致性校验，**不是加密，也不是数据脱敏**。参数、消息和结果仍是敏感存储数据。

ActionObservation 指定追加 sequence 和 result（可为 JSON null），Store 根据 Action 生成
`tool` 消息：`action_id/status/error_code/result`。状态、结果和观察不可分开提交。
序号从 0 开始连续追加，不允许覆盖已有消息；历史 Action 不因下一模型轮次清空当前批次而删除。
幂等键由 Session/Action ID 确定，跨恢复保持稳定，但是否去重仍取决于外部工具。
模型 usage 是当前逻辑调用最近一次结果的供应商无关 JSON；资源预算由调用方按固定单位累积，
本层不推断实际 token、费用或失败请求是否被供应商计费。

所有 Task 写入在开始和提交前检查租约；租约或 token 失效抛 LostLease，修订号/状态/记录冲突抛
StateConflict，非法或超大 JSON 抛 AgentDataError。数据库驱动故障抛 AgentStoreUnavailable，
提交结果可能未知，必须恢复租约后重读，不能直接执行外部调用。不会静默覆盖，异常不携带原始参数输出。
消息、工具参数、单个结果及 usage 各限 100 KB；快照和单次提交限 2 MB；累计会话 JSON 限 8 MB。
活动会话还预留取消/崩溃时记录 UNKNOWN 与观察的容量，防止历史填满后无法安全停止。
只保存有限 JSON，不使用 pickle，拒绝 NaN/Infinity。事件只包含步骤 key、状态、修订号和安全错误码。

普通非 Agent Handler 的 Step 重试/失败行为保留。已有 Session 时：

- 进程在模型预约后退出：新 Worker 接管同一 Session，模型记录为 UNKNOWN，预算/ID 不重置；
  未来 Runner 需先确认结果，才能继续或重试。
- 工具派发后结果未持久化便失去租约：全部 INFLIGHT 转 UNKNOWN，Agent/Step 原子等待对账，
  不自动再次派发。WAITING_RECONCILIATION 不允许任何 INFLIGHT；先对账返回 ACTIONS_PENDING 才能派发。
- Workflow 取消：活动 Agent 转 CANCELLED，未确认工具结果先记 UNKNOWN，释放执行所有权。
- Handler 抛异常或 Step 恢复尝试耗尽：Agent/Step 终态失败，保留未知结果，不套用会重开 Agent 的普通重试。

终态 Agent 快照不可修改；后续到达的结果只能由未来审计/对账服务另存证据。
本 PR 不提供该服务，也不声称仅凭数据库可保证所有外部工具 exactly-once。

验证使用 SQLite 与专用临时 PostgreSQL 的同一组 AgentStore 测试。`scripts/verify_postgres.py`
会运行普通 PostgreSQL、AgentStore 并发/fencing/回滚以及 0001↔0002 迁移测试；SQLite 不模拟行锁。
