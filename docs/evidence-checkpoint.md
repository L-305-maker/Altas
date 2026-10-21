# Evidence-Anchored Context Checkpoint v1

Checkpoint 是确定性原文摘录及来源索引，不是生成式摘要、长期 Memory、RAG 或事实认证。
完整 AgentMessage、Action、Observation、审批、对账及 Usage Ledger 保留在数据库；
只有 Provider 的消息视图发生变化。默认 disabled，既有 full / recent_complete_rounds 不变。
本实现没有额外模型调用、外部读取接口或自动授权。

## 配置与确定性选择

只接受持久 Workflow StepSpec 的 `parameters.agent`，不叠加可热更新的部署策略。
Prompt、Task.inputs、dependencies、工具内容及模型输出都不能改变配置。

```json
{
  "mode": "durable_v1",
  "max_context_bytes": 1000000,
  "context_projection": {
    "mode": "evidence_anchored_recent_rounds",
    "min_recent_rounds": 1
  },
  "evidence_checkpoint": {
    "mode": "anchored_extracts",
    "checkpoint_version": 1,
    "max_evidence_items": 8,
    "max_checkpoint_bytes": 16000,
    "max_excerpt_bytes": 256,
    "min_recent_rounds": 1,
    "result_paths": [["value"]],
    "workflow_identifiers": ["id", "handler"]
  }
}
```

EvidenceCheckpointPolicy frozen、拒绝未知字段。启用摘录必须同时选择 evidence 投影模式，反之亦然。
版本仅支持 1；条数最多 32；checkpoint 最多 64000 字节；单摘录最多 4096 字节。
这些配置不进入 AgentBudget。两处 min_recent_rounds 取较大值；现有轮次不足时全部保留。

1. 完整 ContextBuilder 先检查所有 Action 与 assistant 的有序映射、参数及 Core ID。
   任意旧轮次的 UNKNOWN、PROPOSED 或 INFLIGHT 都阻止模型请求，不能先省略再验证。
2. 可选 workflow_identifiers 仅允许持久 StepSpec 的 id、handler，按配置顺序摘录，默认空。
   随后按原消息 sequence 遍历：首次 user 的结构化 JSON content 中仅摘录原 `goal`；assistant 的
   action_ids 按原批次顺序引用实际 StoredAction。不摘录任意依赖、parameters 或自定义指令字段。
3. 对终态 StoredAction.result 按配置路径顺序取值。路径为最多七层的精确 JSON 对象键，
   不执行表达式、数组索引或通配符。空路径 `[]` 显式选择根结果。
   不存在的路径不产生否定证据；若该 Action 没有任何匹配，保留其原 status 字段。
4. 每项附上原 status 与 error_code；SUCCEEDED、FAILED、DENIED 保持区别。
   以人工对账后的 StoredAction 为准，旧 UNKNOWN 观察仍在审计中，不再充当有效结果。
5. 字符串取 UTF-8 原文前缀；其他 JSON 值先规范序列化再取前缀。前缀只在字符边界结束，
   format 明示 text_excerpt / json_excerpt，truncated 明示是否完整。JSON 摘录只是文本，
   截取后不保证能作为独立 JSON 解析；不会声称它是完整工具结果。
6. 按上述固定顺序选择最早 max_evidence_items 条，记录其余候选数量。
   不按文本语义、模型评分或当前容量临时改选；这样省略旧轮次后仍可保留部分早期证据。
   选定证据全部为本次必需数据，超出 max_checkpoint_bytes 则 EXHAUSTED /
   AgentEvidenceCheckpointLimit，不能继续削减证据以通过。
7. 将 checkpoint 放在第一条保留 assistant 之前的独立 **user** 消息中。
   全部原 system/user 内容、角色和相对顺序保持；assistant/tool 批次不可拆分。
   从最旧完整轮次开始省略，选择能放入总额度的最长连续最近后缀。

Checkpoint JSON 固定标记 `authority=untrusted_data_only`，声明历史摘录没有指令、授权或事实认证权限。
`checkpoint_bytes` 计算该规范 JSON 内容本身；最终 context_bytes 另外计算消息包装、转义、最近轮次和工具 Schema。
若必需内容整体仍超限，EXHAUSTED / AgentContextLimit，不创建 Attempt/Permit，不增加预算或调用 HTTP。

## 引用、Manifest 与请求身份

EvidenceReference 包含 source_type、source_sequence、Action/model_call_id（Action 来源时）、
source_digest、extraction_path、excerpt_digest、evidence_version。
消息来源定位真实 AgentMessage；`payload/content/goal` 表示对 content JSON 解码后的 goal。
Action 来源定位实际 StoredAction，source_sequence 指包含它的 assistant 原消息，
extraction_path 为 `result/...` 或 `action/status`。source_digest 对整个来源 DTO 计算，
包含参数摘要、工具绑定及最终结果，但 Manifest 不复制其正文。
workflow_step 来源绑定当前 Session 所属真实 StepSpec，source_key 为 StepSpec.id；
此来源是单一记录，source_sequence 固定 0，path 为 id 或 handler。
SQLAgentStore 使用锁定 Run 内的定义重算，不能接受调用方伪造 StepSpec；摘要覆盖完整定义，摘录只输出选定标识。
excerpt_digest 对 `{format, excerpt, truncated}` 计算；status/error_code 还受来源和内容摘要保护。

EvidenceCheckpointManifest 包含版本、policy_digest、source_history_digest、evidence_reference_digest、
checkpoint_content_digest、checkpoint_bytes、included/omitted_evidence_count，以及有界 references。
摘要统一使用紧凑、排序键、ensure_ascii=False、allow_nan=False 的 JSON UTF-8 SHA-256；
数组顺序不变，不进行 Unicode 归一化。source_history_digest 覆盖完整原始 messages 和最终 actions，
与 ProjectionManifest 的规范回放 full_context_digest 区分。
选择 Workflow 标识时 source_history_digest 还包含实际 StepSpec；未选择时不加入。
引用列表摘要的对象是 `{"references": [...]}`；其数量和摘要由类型验证。

Runner 先生成完整历史、摘录和投影，再进行协议/精确字节校验与 Provider 能力/Token 预检，
之后才检查硬调用预算、Observed Usage、Provider Admission。
SQLAgentStore 持有原 Run → Step → Session → ModelCall → Attempt 锁，在 Permit 前重读来源、
运行完整 ContextBuilder、重建摘录与投影，逐字段比较完整 Manifest，并验证 RequestContract 的
投影摘要和实际消息/Schema。仅提交一个自洽摘要不能绕过来源核对。
Manifest、Attempt、RequestContract、Snapshot 和 Permit 同事务预约；HTTP 在提交且重读所有权后执行。

同一逻辑 FAILED 调用重试保持 model_call_id，增加 attempt_index，整个 ProjectionManifest（含 checkpoint）
和 RequestContract 必须相等。来源、策略或 Schema 漂移均在新预约/Permit 前 fail-closed。
已预约 MODEL_INFLIGHT 在恢复时仍 UNKNOWN/fail-closed；确认丢失不会重发，也不通过新 token 伪造所有权。
已有 Attempt 的来源可按其 model_call_id 对应 assistant 响应之前的原历史边界定位；
checkpoint 不替代原历史，后续新消息也不会覆盖旧 Manifest。

## 持久化兼容与容量

复用 0007/0008 的 agent_model_attempts.context_projection JSON，增加可选 checkpoint。
不新增迁移，不改 0001–0008。没有 checkpoint 时序列化完全省略该键，旧 JSON 和请求摘要不变；
旧数据不回填，默认恢复为 None。旧二进制不能读取新 mode/字段，因此启用新模式前应升级全部 Worker；
可选 workflow_identifiers 为空、source_key 为 None 时省略其键，已有 v1 checkpoint 摘要也保持不变。
回滚二进制前需停止 Writer 并处置这些会话，不能假定旧 reader 支持新 JSON 模式。
不改变数据库事务、Action Approval 或调度规则。

有界引用及 Manifest 计入既有 8 MB Session 配额；读取单个扩展 Manifest 上限为 64000 字节。
预约仍预留未知结果、取消、审批及对账控制空间。若不能容纳新增元数据，Store 事务整体回滚，
Runner 按原 AgentStorageLimit 路径安全结束，不创建孤立 Attempt/Permit。
正常 upgrade head 与 pg_dump/pg_restore 保留全部旧事实与新 JSON；人为 downgrade 到 0006
仍会按原迁移删除 Projection，downgrade 到 0007 会删除 RequestContract，不能声称这种降级无损。

## 权限及限制

恶意文本（ignore previous instructions、reveal secrets、change tool arguments、execute this command、
bypass approval）可以作为带引用的原文数据存在，但不会改写 system、ToolBinding、Action ID、
arguments_digest 或审批状态。执行仍经过 ToolRuntime 白名单、Schema、行动审批与 Store fencing。
这是权限边界保护，不是保证模型不会被不可信文本误导；摘录不能证明原文在现实世界正确。
Manifest/事件/指标不记录 Prompt、摘录正文、工具结果或凭据，不增加日志或指标标签。

固定条数和前缀会损失语义，不能保证选中最有用证据；较早证据也可能过时。
Checkpoint 可能增加短历史请求体积；默认 disabled，启用需针对工作负载评估。
max_context_bytes 不是精确 Token 窗口、硬账单上限或实际费用结算；仍保留现有 TokenCounter 保证等级。
外部模型/工具不保证 exactly-once。没有真实供应商效果或事实准确性验收。

若未来另做 LLM-assisted Summarization，应有独立提案：显式授权与计次计费、单独持久 Attempt、
每项结论的来源引用/不确定性、不可覆盖的派生版本、非特权消息承载，以及失败后原始历史回退策略。
摘要不能授予工具权限或替代 UNKNOWN 对账；本阶段不实现也不触发此类请求。
