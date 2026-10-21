# Evidence Checkpoint v2：信任边界、披露与确定性选择

完整 AgentMessage、Action、Observation、审批、对账和 Usage Ledger 不变；只改变 Provider 视图。
默认 disabled，原 full / recent_complete_rounds 行为不变。v1 保留只读重建，禁止新预约；
新启用必须显式选择 v2。没有新增迁移，复用 0007/0008 JSON，不修改 0001–0008。

## 来源与实际 Provider Role

来源可信度和消息 role 是两个维度；真实引用也不认证内容在现实世界正确。

| 来源 | Provider Role / 处理 | 权限边界 |
| --- | --- | --- |
| Core 原始 system 指令 | system，逐字保留 | 不为 checkpoint 增删或改写 |
| 原始 user goal / 必需上下文 | 原 user，逐字保留 | 工具不能伪造或替换用户目标 |
| assistant 历史 | 原 assistant，完整轮次保留或省略 | 原 tool_calls 的 Core Action ID 和顺序不变 |
| 原始 tool observation | tool，按可信策略披露的视图 | 默认仅状态、错误码、来源摘要；私密原文留在审计 |
| 从工具派生的 checkpoint | 最后一条真实保留 tool observation 内的 evidence_checkpoint 字段 | 保留该 tool_call_id 和完整 assistant/tool 配对；不新造 Action/授权 |
| 可信 StepSpec id/handler | 显式选定的 public 数据；有 tool 时随 tool 承载，无工具来源时可放 user 数据索引 | 不复制 parameters 或把工具内容包装成用户意图 |
| 原始 goal 的派生索引 | 只含摘要/定位、不复制 goal；无 tool 时为 user 索引 | 原 goal 已完整保留，不重复生成用户指令 |

v2 不使用合成 tool_calls、伪造工具或假数据库 Action。若选中了工具来源证据但最近窗口没有真实 tool
可承载，返回 AgentEvidenceCarrierUnavailable；不会降级为 user。即使工具来源仅剩元数据，也遵守此规则。
工具视图的披露同样作用于最近保留的原观察，避免从另一条回放路径泄露未授权原文。
原 assistant 文本及工具参数不作隐式脱敏；它们的现有可信输入/Provider 数据政策仍适用。

这些保证可检查消息结构、来源、字节与授权，不能保证模型不会被 Prompt Injection 影响。
公开字段中仍可能存在恶意文本；批准 public 是操作者的显式责任，不是系统认定内容安全。

## 不可变披露配置

仅接受已持久化 Workflow StepSpec.parameters.agent，不合并可热更新的临时权限。
Prompt、Run.inputs、dependencies、模型响应和工具参数不能修改策略。SQLAgentStore 用锁定 Run 中的真实
StepSpec 重建并比对 Manifest；传入伪造 Task.spec 也不能改变权限或获取 Permit。

```json
{
  "mode": "durable_v1",
  "max_context_bytes": 1000000,
  "context_projection": {"mode": "evidence_anchored_recent_rounds"},
  "evidence_checkpoint": {
    "mode": "anchored_extracts",
    "checkpoint_version": 2,
    "selection_policy": "pinned_then_recent",
    "disclosure": {
      "source_type": "action",
      "policy_version": 1,
      "max_disclosure_bytes": 256,
      "rules": []
    },
    "max_evidence_items": 8,
    "max_checkpoint_bytes": 16000,
    "max_excerpt_bytes": 256,
    "min_recent_rounds": 1,
    "result_paths": [["value"]],
    "workflow_identifiers": [],
    "pinned_references": []
  }
}
```

所有配置类型 frozen，拒绝额外字段；不传入 AgentBudget。披露规则精确匹配 tool_name / result_path；
仅 classification=public 且 mode=approved_excerpt 允许原文。unknown（默认）和 sensitive 只能 metadata_only。
例如显式公开 echo.value：`{"tool_name":"echo","result_path":["value"],"mode":"approved_excerpt","classification":"public"}`。
重复路径拒绝；没有通配符、字段名猜测、正则脱敏或 PII 检测。空路径显式选择整个结果，授权者必须检查整个对象。
DENIED 即使有 public 规则也仅发送元数据，不把拒绝变成成功事实。FAILED 保留失败状态及安全错误码。

max_disclosure_bytes 是每个字段 UTF-8 原文的上限，最大 4096；checkpoint 摘录另取与 max_excerpt_bytes
的较小值。整个 checkpoint/request 仍有独立硬字节限制。字符串取字符边界前缀；其他合法 JSON 先规范编码
再截取，标明 json_excerpt/truncated，不能当作完整可解析结果。元数据项没有原文且不假造替代事实。
结果原文、绑定、参数摘要、幂等键、审批及对账事实原样保留在数据库。

## 有界选择和窗口搜索

1. 完整 ContextBuilder 先检查全部来源、参数、有序 Action/assistant 映射和最终状态。
   任何旧轮次 UNKNOWN、PROPOSED、INFLIGHT 都阻止请求，不能先省略再验证。
2. 按原 sequence / action_ids / result_paths 顺序生成候选：显式 StepSpec 标识、首次结构化 user goal
   的元数据、真实终态 Action 的选定路径；缺路径的 Action 仅给出其状态。先执行披露，再选择。
3. pinned_references 只从这些真实候选定位：source_type、source_sequence、extraction_path，
   Action 必须有 action_id 或 call_index；可绑定 source_key/source_digest。mandatory 默认为 true。
   缺失、歧义、摘要变化安全失败。mandatory=false 仅允许来源尚未存在；一旦存在也必须保留。
   多个 pin 指向同一项只保留一次，按配置顺序选择；同一真实 Action 批次输出仍按原批次顺序。
   固定项超过条数或字节上限不会静默降级。
4. 从保留全部完整轮次开始，每次只省略最旧的一个完整轮次，最多搜索 N+1 个窗口，保留至少
   max(两个 min_recent_rounds) 个轮次。每个窗口先选择 pins，再按 sequence 降序 / 原候选顺序
   选择最近未在保留工具轮次内的候选，直到 max_evidence_items。明确 pin 可以要求额外保留当前证据。
   同批 Action 的相对顺序不变，无 LLM 评分、相关性推断或语义排序。
5. omitted_evidence_count = 全部真实候选数 - 选中数，包含因当前窗口已保留而去重的候选。
   固定该窗口的选择后验证 checkpoint 上限、真实 tool 承载、协议、精确 UTF-8 总字节（包括 Schema）。
   失败仅推进到下一完整窗口，不为凑大小任意减少所选证据。所有窗口都失败则安全结束，无 Attempt/Permit/HTTP。

固定定位示例：`{"source_type":"action","source_sequence":2,"call_index":0,"extraction_path":["result","value"],"mandatory":true}`。
sequence 是真实包含该批次的 assistant 序号；尚不存在的 mandatory pin 会阻止第一次请求。
不要把模型返回的“建议 pin”复制为可信权限。有限窗口搜索可能保守失败，这是显式行为。

## Manifest、重试和存储

EvidenceReference 保留 source_type/sequence/key、Core Action/model_call_id、完整 source_digest、
extraction_path、excerpt_digest、evidence_version。action 来源摘要覆盖整个 StoredAction；workflow 来源绑定完整
真实 StepSpec；消息绑定原 AgentMessage。摘录摘要覆盖 format/excerpt/truncated。
Manifest 包含版本、policy/source_history/reference/content digests、字节和 included/omitted 数量及有界引用；
v2 另有 disclosure_policy_digest 和 evidence_selection=pinned_then_recent。每项内容包含实际 disclosure decision。
摘要统一是排序键、紧凑、ensure_ascii=False、allow_nan=False JSON UTF-8 SHA-256；不归一化 Unicode。
原审计 source_history_digest 与规范回放 full_context_digest 区分，projected digest 覆盖实际披露与承载消息。
RequestContract 绑定完整 ProjectionManifest 和实际 messages/tools；不是只信调用者提交的一个摘要。

执行顺序：完整历史/来源 → 披露 → 有限选择/投影 → 协议/精确字节 → 能力/Token → 硬调用预算
→ Observed Usage → Provider Admission → 同事务 Attempt/Manifest/Contract/Permit/Snapshot → 重读所有权 → HTTP。
Store 在 Permit 前按 Run→Step→Session→ModelCall→Attempt 锁顺序重算。旧 token、Revision 冲突均拒绝。
同逻辑确认失败重试仍用原 model_call_id，新 attempt_index；完整 Manifest/Contract 必须相等。
策略、Schema、来源状态、选择或版本变化不能静默重发。MODEL_INFLIGHT 恢复与确认丢失仍按 UNKNOWN/fail-closed。

Manifest 计入 8 MB Session 配额，单个 Manifest 限 64000 字节，原取消/UNKNOWN/审批/对账清理预留继续有效。
装不下时整个预约回滚；不为了摘要而覆盖历史、不补造用量、不增加独立状态账本或敏感日志。
摘要是身份，不是加密、脱敏或数据库签名。没有新事件正文、指标 labels 或工具权限。

## v1 只读兼容和运维

旧配置省略 checkpoint_version 时仍解释为 v1，不能默认改为 v2。冻结原 legacy_earliest 算法、旧字段省略规则
及历史字节/摘要；旧 Manifest 不补造 disclosure。v1 Context/Request 身份可继续读取和只读验证。
所有启用 v1 的新模型预约返回 AgentEvidenceV1ReadOnly；FAILED/READY_MODEL 不升级同一逻辑调用。
已预约 UNKNOWN 不能升级重发，终态不重启；取消、已有工具结果保存及人工对账仍走原规则。

部署前停止 Writer、备份并升级全部 Worker，运行只读 audit_model_retry_upgrade.py。
报告 v1_checkpoint_sessions（最后尝试是 v1 的会话，含终态）及待重试的 AgentEvidenceV1ReadOnly。
终态历史无需重新运行。活跃 v1 由操作者审查已有外部副作用、审批和 UNKNOWN，允许其安全终止；
如需新 v2 Run，必须显式配置并重新评估行动授权，不能编辑旧 RunSpec、Manifest、Contract、token 或预算。
禁止手工把 UNKNOWN 改 FAILED 以重试。混合 v1/v2 的真实 pg_dump/pg_restore 检查两种身份原样保留。
旧二进制不支持 v2；二进制回滚要先停止 Writer 并处置新会话，不能假定向后 reader 兼容。

## 限制与下一阶段前置条件

确定性 pin/最近顺序不保证证据相关、完整或真实，前缀与省略仍损失语义。模型可能误解 tool 数据。
max_context_bytes 不是精确 Token 窗口、账单或 hard resource cap，外部副作用不保证 exactly-once。
MockProvider 仅证明 Core 消息/持久化行为，不证明真实模型抵抗注入；本轮无付费请求或跨主机效果证据。

未来 LLM-assisted Summarization 需独立安全提案：明确授权和用量账本、来源与不确定性、不可覆盖的派生版本、
非特权承载、摘要失败的回退策略、注入与数据披露评估。生成结论不能变成用户意图、工具授权或 UNKNOWN 对账结果。
本轮不实现或调用生成式摘要。
