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
未选择 Durable 模式的 AgentRunner 继续保持原有行为；不提供等待步骤的通用恢复入口。

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
  DurableRunner 将其保留为 UNKNOWN 并 fail-closed 终止，不静默重发。
- 工具派发后结果未持久化便失去租约：全部 INFLIGHT 转 UNKNOWN，Agent/Step 原子等待对账，
  不自动再次派发。WAITING_RECONCILIATION 不允许任何 INFLIGHT；先对账返回 ACTIONS_PENDING 才能派发。
- Workflow 取消：活动 Agent 转 CANCELLED，未确认工具结果先记 UNKNOWN，释放执行所有权。
- Handler 抛异常或 Step 恢复尝试耗尽：Agent/Step 终态失败，保留未知结果，不套用会重开 Agent 的普通重试。

终态 Agent 快照不可修改；后续到达的结果只能由未来审计/对账服务另存证据。
本 PR 不提供该服务，也不声称仅凭数据库可保证所有外部工具 exactly-once。

验证使用 SQLite 与专用临时 PostgreSQL 的同一组 AgentStore 测试。`scripts/verify_postgres.py`
会运行普通 PostgreSQL、AgentStore 并发/fencing/回滚以及 0001↔0002 迁移测试；SQLite 不模拟行锁。

## DurableAgentRunner（PR3）

仅在 `StepSpec.parameters.agent.mode = "durable_v1"` 时启用。CLI 将现有 Worker Store
传入组合根，`SQLAgentStore(store)` 复用连接池；同步数据库调用全部在线程中执行。
旧的两参数 handler_factory 继续可用；支持 `store` 关键字的工厂会收到共享 Store。
不指定模式时仍使用原 AgentRunner，不改变普通 Handler 的返回值和重试规则。

```json
{"prompt":"Summarize the supplied data", "tools":["read_text"],
 "agent":{"mode":"durable_v1", "max_model_rounds":8,
          "max_model_attempts":12, "max_tool_calls":32}}
```

Agent ID 由 Run/Step 身份确定；初始化目标和依赖输出作为 user 数据持久化，恢复时验证
同一初始化。系统消息仅由应用生成。预算和 Action 身份从持久状态恢复，不按 Worker 尝试重置。
PR3 强制模型轮次、请求尝试和工具派发次数预算；usage 保留供应商报告值，未知值为 null。
没有实现费用或 token 的资源计价策略，因此 Runner 的 `max_resource_units` 只接受 0，
表示未启用该项计价，不能将其宣称为费用上限。Domain/AgentStore 的独立资源结算能力保持不变。

每次模型请求先原子预约 MODEL_INFLIGHT，提交后再核对当前租约并发出请求。响应、模型记录、
有序 Action 和 revision 原子保存。工具顺序执行；先校验白名单、Schema 和部署时声明的副作用策略，
再预约 INFLIGHT 并增加 tool_calls，提交后核对租约才派发。派发前必须留有下一模型轮次和请求预算，
因此最后一轮的工具提案会耗尽终止，不先产生副作用。合法最终回答只接受无工具调用、非空文本及
finish_reason=stop；length 截断不能完成。成功仍由 AgentStore 原子提交 Agent、Step 和 Artifact。

`ModelProvider.complete()` 保持旧消息协议，Durable 模式使用 `respond() -> ModelResponse`，
包含 assistant_message、finish_reason、usage、model 和 request_id。DeepSeek 不做内部重试；
MockProvider 可以注入类型化响应和明确错误。MockTransport 测试只验证 HTTP 契约，不代表真实模型联调。

### 审计历史与模型上下文

assistant payload 保存规范消息、finish_reason、供应商元数据和有序 `action_ids`。
核心 Action ID 由 Runner 生成；供应商 tool_call ID 原样留在该条消息内，映射作用域是
`(model_call_id, call_index)`，不要求供应商 ID 跨轮唯一。恢复派发前也校验映射和参数，
缺失映射的 PR2 手工会话无法安全升级执行，必须停止，不能猜测 ID 或重新生成行动。

AgentContextBuilder 在每次模型调用前重建上下文。assistant 的工具调用 ID 替换为核心 Action ID，
对应 tool 响应也使用相同 ID。每个已解决 Action 只按最新 StoredAction 结果回放一次，
按模型轮次及 call_index 排序；数据库中 UNKNOWN 和后续对账观察全部保留。
任何未解决 Action（包括 UNKNOWN）均阻止下一模型请求。工具输出始终作为 tool 内容编码，
不会将输出中的 role、action_id、status 或审批字样解释成系统指令或执行权限。

非法 JSON、重复参数键、非对象或非有限参数保留原始 assistant 调用；对应 Action 使用空参数摘要
作为不可执行的拒绝记录，invalid_arguments 标记其格式错误。每次恢复派发都重新解析原始参数，
不依赖该标记授权执行。Action 随后转 DENIED，附安全结构化错误；修正必须来自下一轮的新 Action。
参数摘要用于一致性校验，不是加密或脱敏。消息、参数、结果仅进入受限持久存储，不进入事件或日志。

### 错误与恢复矩阵

| 情况 | 持久化结果 | 后续行为 |
| --- | --- | --- |
| 已确认可恢复模型失败（429、连接建立失败） | ModelCall failed，READY_MODEL，安全错误码 | 同一逻辑 ID 有界重试，增加 attempts |
| 模型读取超时、5xx、未知异常或恢复中的模型预约 | ModelCall unknown，Agent/Step FAILED | 不重发；请求可能已被供应商计费 |
| 模型配置、无效响应协议、空/截断回答 | ModelCall failed，Agent/Step FAILED | 不重试，不保存异常正文 |
| 参数格式/Schema 错误 | Action DENIED，tool_calls 不增加 | 安全错误反馈模型，在总预算内创建新 Action |
| 未授权、需审批、UNSAFE 或策略变更的工具 | Action DENIED，tool_calls 不增加 | 不执行；Step 级审批不授予后续行动权限 |
| ToolConfirmedFailure（适配器确认的失败） | Action FAILED、观察原子保存 | 反馈模型；已失败 Action 不重新派发 |
| 工具超时、未知异常、非法/超大结果、执行中取消 | Action UNKNOWN，Agent/Step 等待对账 | 不再请求模型或派发此行动 |
| 调用次数预算耗尽 | Agent EXHAUSTED、Step FAILED | 不重开 Agent，不走整步异常重试 |
| LostLease / StateConflict / AgentStoreUnavailable | 不尝试覆盖、不走普通 Store.fail | 停止；由租约恢复重读持久事实 |

Tool 默认副作用为 UNSAFE。Durable 模式只允许部署者显式声明 READ_ONLY、IDEMPOTENT 或
RECONCILABLE 且在当前白名单中的工具；requires_approval 的工具一律拒绝。
`Tool.invoke_durable(arguments, ToolInvocation)` 可接收稳定 Action ID 和幂等键；
普通 invoke 保留兼容性。工具适配器只有确认失败时才抛 ToolConfirmedFailure，其他执行异常视为 UNKNOWN。
目前不提供行动审批 API，也不从 StepSpec.requires_approval 推导行动授权。

Worker 接受绑定当前 Task 的 CommittedAgentResult，并读取数据库核对 Agent revision、终态/等待状态、
Step 状态及已释放租约，随后不再 complete/fail。不存在提交的标记会被拒绝；并发取消造成的状态变化
作为冲突停止处理。租约失效或提交结果未知不转成普通 Handler 失败。模型/工具执行中的协程取消会在
仍持有有效租约时保守记录 UNKNOWN；若租约已失效，由 Store 的取消/过期恢复事务记录 UNKNOWN。

仍存在数据库提交与外部派发之间的不可原子窗口：租约核对不能撤回已经发出的请求，不能保证外部
exactly-once。崩溃在预约后、派发前也可能保守进入 UNKNOWN。终态不可重开；晚到结果需另存审计证据。
PR4 必须补充有鉴权的行动审批、对账证据与等待状态恢复事务，先解决 UNKNOWN 并返回 ACTIONS_PENDING，
才可派发尚未执行的 Action。PR3 没有新增任意恢复 waiting Step 的入口。

临时 PostgreSQL 验证脚本同时执行新 Runner/Worker 集成、接管 fencing 和真实子进程退出测试。
真实模型 API、Docker sandbox 和 Compose 仍需各自的环境验证，不能用 SQLite 或 MockTransport 代替。
