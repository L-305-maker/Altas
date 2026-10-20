# Durable Context Projection v1

Projection Manifest 的身份现由 [ModelRequestContract](model-request-contract.md) 与 Provider/model/输出参数绑定。
0008 在 Attempt 增加可空请求契约，保留本页的投影语义；旧失败调用的升级风险可通过只读核查脚本识别。

完整持久历史仍是唯一恢复与审计事实。投影只决定一次 Provider 请求的消息视图，既不删除消息，也不修改
Action、工具结果、审批、对账、预算、Usage Ledger 或 Artifact。默认 `full` 保持原有消息与字节限制。

## 配置与执行顺序

配置只来自已持久化 Workflow StepSpec 的 `parameters.agent`，例如：

```json
{
  "mode": "durable_v1",
  "max_context_bytes": 1000000,
  "context_projection": {
    "mode": "recent_complete_rounds",
    "projection_version": 1,
    "min_recent_rounds": 1
  }
}
```

`ContextProjectionPolicy` 是 frozen、拒绝额外字段的类型。当前只执行版本 1，未知版本拒绝执行。
`min_recent_rounds` 是正整数；初始历史少于该数目时全部保留。这些字段不传入 AgentBudget。
Prompt、Task.inputs、依赖数据、工具结果和模型输出不能更换策略或提高限制。

执行顺序是完整 Session 恢复 → AgentContextBuilder 完整验证 → 投影与再次协议验证 → 精确字节限制
→ 硬调用预算 → Observed Usage Governance → Provider Admission → Attempt/Manifest 原子预约 → HTTP。
数据库操作继续在线程中卸载，HTTP 不进入事务。

ContextBuilder 检查所有历史 Action 与 assistant 消息的模型调用、参数、有序映射和 Core Action ID。
完整历史中任何未完成或 UNKNOWN Action 都阻止模型请求，即使该轮原本可以被省略。
历史 UNKNOWN 观察和人工确认观察继续追加保存；Provider 只看到 StoredAction 的一个最终有效结果。

## 选择算法

1. 所有原始 system 和 user 消息都保留在原相对位置，内容及角色不变。
2. 每条 assistant 普通消息是一个轮次；带工具调用的 assistant 及其全部有序 tool 响应是一个不可拆轮次。
   assistant 的工具 ID 和响应的 tool_call_id 都使用 Core Action ID，跨模型轮次的供应商 ID 不参与猜测。
3. `full` 不移除轮次。`recent_complete_rounds` 从最旧轮次开始移除，选择满足字节限制的最长连续轮次后缀，
   至少保留 min_recent_rounds（若历史有足够轮次）。不会从中间跳选，也不会切开多工具批次。
4. 在选择前及选择后检查消息协议：无孤立工具观察、调用 ID 不重复、响应 ID 与顺序一致。
5. 必须保留的 system/user、Schema 或最小完整后缀无法放入时，提交 EXHAUSTED / AgentContextLimit。
   不截断任何字段，不请求摘要，不在模型上下文中推断或解释被省略的工具结果。

不插入摘要或省略标记。投影不是 LLM Summary、长期 Memory 或 RAG，无法恢复被省略内容的语义。
模型可能因为缺少早期上下文而改变决策，因此 recent_complete_rounds 必须显式选择，默认仍为 full。

## 身份与可重现性

`ContextProjectionManifest` 包含 projection_version、selection_policy、min_recent_rounds、max_context_bytes、
full_context_digest、projected_context_digest、projected_context_bytes、included_round_count、omitted_round_count
和 tool_schema_digest。它只包含配置、数量与 SHA-256，不包含 Prompt、工具结果、凭据或供应商 request_id。

规范化摘要规则：`json.dumps(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))`
的 UTF-8 字节取 SHA-256。数组顺序保留；字符串不做 Unicode 归一化或语义改写。
完整及投影视图摘要的对象都是 `{"messages": [...], "tools": [...]}`，工具为空时省略 tools 字段。
tool_schema_digest 单独对工具数组计算，空数组也有确定摘要。
消息字符串内部的 JSON（例如工具观察）保持 ContextBuilder 生成的文本，不进行二次解释。
full_context_digest 表示**完整规范回放视图**，不是整个原始审计数据库的摘要。

字节计量沿用 context_bytes：同一 payload 用紧凑 JSON、ensure_ascii=False、allow_nan=False 编码成 UTF-8；
计入所有消息和工具 Schema。恰好等于上限时可执行，多字节中文和 emoji 按实际编码计数。
供应商的模型名、认证头等适配器字段不在该 Core 上下文额度内；这不是 HTTP 整包或精确 Token 上限。

Runner 对工具 Schema 做独立快照，避免预约期间部署热更新改变实际发送内容。
预约携带类型化 ContextProjectionReservation（Manifest + 临时 Schema）；Store 在锁定的完整状态和
持久 StepSpec 下重新计算，且在申请 Provider Permit 前检查一致性。Schema 本身不会重复持久化。
Manifest 与 Snapshot revision、逻辑 ModelCall、ModelAttempt 和许可在同一事务中提交。

同一逻辑调用已确认失败后，重试复用 model_call_id，新建 attempt_index；完整 Manifest 必须与前次相同。
策略、版本、最大字节数、最少保留轮次、历史或 Schema 改变时提交 FAILED / AgentContextProjectionChanged，
不增加尝试、不申请新 Permit、不发送请求。历史未提供 Manifest 时无法证明相同，Runner 同样保守终止重试。
AgentContextProjectionInvalid 表示投影协议/契约不合法；不会把它解释成可重试的模型失败。

数据库确认丢失后重新读取 Snapshot/Attempt；MODEL_INFLIGHT 的外部结果按原有 UNKNOWN 策略处理，
不会为了核对上下文而重发。旧 token 和旧 revision 仍分别由 Lease/Fencing 与 CAS 拒绝。
完整历史损坏时不能伪造一个合法的终态提交；既有 Store 冲突与租约恢复路径继续 fail-closed。

## 迁移、容量与兼容性

0007_context_projection（down_revision=0006）仅为 agent_model_attempts 增加可空 JSON 列 context_projection。
不修改 0001–0006，不回填旧投影，不改变原有用量。旧尝试恢复为 None。降级到 0006 会移除该列及 Manifest，
保留消息、Action、审批、对账、用量和 Artifact；重新升级不能恢复已删除的 Manifest。
正常 upgrade head 和 pg_dump/pg_restore 均保留已有 Manifest。升级应停止旧 Writer/Worker、备份后迁移。

新增 Manifest 计入 8 MB 会话额度，None 不向旧会话追加容量收费；预约检查保留原有 UNKNOWN、取消、
有界结果和人工审计的控制空间。投影降低 Provider 上下文体积，不降低持久化历史占用，存储额度仍可能耗尽。
旧直接 Store 调用在未显式配置投影且没有已绑定 Manifest 时仍可省略该字段；DurableAgentRunner 每次预约
始终提供 Manifest。显式配置或已绑定投影的调用不能走旧接口绕过验证。

摘要是可核对的内容身份，不是加密、脱敏或恶意数据库写入的签名。禁止在事件、日志和指标 labels 中输出正文；
本功能不新增遥测，也不把 Manifest 用作成本或计费事实。

## 验证入口与限制

- tests/unit/test_context_projection.py：完整历史校验、UTF-8/Schema、连续完整轮次、终态工具配对、版本与确定性。
- tests/integration/test_durable_context_projection.py：真实 Worker/Store、重试、审批/UNKNOWN 对账、准入/用量门控、
  Schema 热更新、提交确认丢失、容量清理，以及 PostgreSQL 独立事务 CAS。
- tests/integration/test_context_projection_migrations.py：SQLite/PostgreSQL 升降级、旧数据保留及 None。
- 既有进程崩溃、五秒取消清理、fencing、下游 DAG、RC 三进程 Soak、备份恢复和 Wheel 检查继续执行；
  通用不变量额外检查 Manifest 在结果确认前后都不可改变。

实际执行结果见 RC checklist 与本次交付记录。单机多进程不能证明跨主机网络分区安全；MockProvider/MockTransport
不是供应商实测。本轮不执行付费请求。字节额度不证明供应商 Token 窗口足够，省略语义风险需按任务评估。
