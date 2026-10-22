# First-Class Durable Summarization v1

后续 [Summary Consumption v1](summary-consumption.md) 允许独立授权的真实只读工具消费；
默认行为仍不自动注入。生成披露不等于消费授权，派生 Artifact 与原历史保持分离。

Summary 是独立 Workflow Step 发布的 **派生 Artifact**，不是权威历史或事实认证。
不替换 AgentMessage/Action，不自动注入后续 Agent Context，不实现 Memory、RAG 或 Planner。
默认没有摘要请求；必须显式注册 `durable_summary` Step、`artifact=true`、依赖来源并设置 enabled=true。

## 显式执行与配置

```json
{
  "id": "summary",
  "handler": "durable_summary",
  "depends_on": ["source"],
  "artifact": true,
  "parameters": {
    "summary": {
      "enabled": true,
      "summary_version": 1,
      "source_step": "source",
      "max_summary_bytes": 16000,
      "max_summary_model_rounds": 2,
      "max_summary_model_attempts": 3,
      "max_source_bytes": 4096,
      "max_context_bytes": 32000,
      "required_evidence_coverage": 1.0,
      "allow_uncertain_claims": false
    }
  }
}
```

`source` 必须是同一 Run 中真实、已 succeeded 且存在成功 AgentSession 的依赖 Step。
Task.step_id 是 StepRow.key，来源从锁定 Run 的真实 StepSpec 按 key 解析到 StepRow UUID；
没有接受任意 Session ID 的模型工具或公开恢复接口。Task.inputs、Prompt、依赖输出不能提高权限或预算。
v1 不接受摘要的递归来源，也不摘要纯 user/assistant 正文；没有可选行动证据时 SummarySourceEmpty。

DurableSummaryPolicy 是 frozen/extra=forbid，enabled 默认 false；版本严格为整数 1。
输出上限 256–32000 字节，模型轮次 2–4，尝试 2–8 且不小于轮次，证据输入 256–4096 字节，
最终上下文 1024–100000 字节。覆盖阈值为有限 [0,1]，开关严格布尔；重试基础/最大等待为有限
(0,3600] 秒，基础不得超过最大值。普通 agent 参数不能另行覆盖 Summary 的派生配置。

策略随不可变 Workflow 版本和 Run 定义保存。摘要 Session 首次 open 时，Store 在有效租约事务中
重算来源和精确初始化消息，保存冻结 SummaryInput；不能先保存无约束初态再切换权限。
恢复仍比较冻结策略/来源。不同摘要 Step 是独立请求；真正租约失败保留原 Step 重试上限和延迟。

bootstrap 注册该 handler，复用 Worker 的 Store/Engine、Provider 和部署准入/用量策略。
显式 mock 模式使用 MockSummaryProvider：仅输出固定协议声明和真实可用引用，不模拟语义质量。
DeepSeek 模式仍需操作者自行提供受保护凭据；本轮没有执行任何真实付费请求。

## 来源与披露

SummarySourceSnapshot 记录真实 source Run/Step/Session、revision、完整来源与 StepSpec digest、
披露身份、证据引用 digest、允许来源类型、稳定边界时间、终态和 selected_evidence_only 完整性标记。
created_at 是源 Session 最终更新的数据库时间，代表来源发布边界；摘要生成时间另在 Artifact 中保存。
相同稳定来源、策略和目标 Step 会生成相同 Snapshot/Manifest，不以当前 Worker 时间重新定义来源。

执行顺序：完整来源恢复和验证 → Action/assistant/tool 真实映射 → 历史 ToolBinding → Egress 与
Evidence v3 授权 → 确定性选择/字节限制 → SummaryInputManifest → Capability/Token Preflight
→ 硬调用预算/Observed Usage/Provider Admission → 原子预约 → 重读所有权 → Provider。

`source_egress` 默认 metadata_only；`source_evidence` 默认 v3、空披露规则。
两者的 EvidenceDisclosurePolicy 必须完全一致。公开字段需完整历史 binding、精确路径、public 和
approved_excerpt，不能用当前 registry 替换历史身份。若源 Step 显式设置了 Egress 或 Checkpoint，
摘要公开授权还必须与每份源披露策略完全一致；metadata-only 可以更严格。不能通过摘要扩大源 metadata_only、
sensitive 或旧未绑定策略的公开权限。不确定等价性时 SummarySourceDisclosureConflict，预约前停止。
源无显式策略时也不继承 legacy_full 原文权限，仍必须提供独立、完整绑定的摘要公开规则。

可信注册入口构造公开规则后，将相同授权用于源执行和摘要来源（示意）：

```python
binding = runtime.binding("read")  # 已注册工具的实际稳定版本与策略
rule = {"tool_name": "read", "binding": binding.model_dump(mode="json"),
        "result_path": ["public_value"], "classification": "public", "mode": "approved_excerpt"}
summary_parameters = {
    "enabled": True, "source_step": "source",
    "source_egress": {"disclosure_mode": "bound_disclosure", "rules": [rule]},
    "source_evidence": {"mode": "anchored_extracts", "checkpoint_version": 3,
        "selection_policy": "pinned_then_recent", "disclosure": {"policy_version": 2, "rules": [rule]}},
}
```

v1 来源仅有获准公开的行动字段摘录及真实行动状态元数据。私密结果路径不作为声明引用；未获准结果
只能引用 action.status，不携带其原文。原始 goal、system、assistant/reasoning、工具参数、审批 actor/证据正文
不会复制到摘要请求。源历史 UNKNOWN/未完成行动先阻断，不能通过省略旧轮次隐藏。

使用已有 v3 摘录和确定性最近选择规则；每份引用包含原始 source digest、路径和历史绑定身份。
v1 不接受 workflow_identifiers 或 pins。选定证据包含 excerpt/truncated，不能将有限摘录当完整原文。
最多选择 32 项，再校验工具响应实际规范 UTF-8 JSON 总额最多 4096 字节；过大安全失败，不自动删项或
扩大披露。完整审计和未选择/未披露内容留在源 Session，没有压缩覆盖。

## 真实证据工具与消息角色

摘要初始 system/user 消息只有 Core 固定任务/输出 Schema，不复制 Task.dependencies。
冻结 SummaryInput 存在初始化消息的附加持久字段，ContextBuilder 不将它作为 content 回放。
模型必须真实调用 `read_summary_evidence({})`。工具仅在此 Task 的有效所有权下返回已冻结、获准的证据，
无 Session 参数、无外部写操作；正常经过 ToolRuntime 白名单/Schema、Action PROPOSED→INFLIGHT 预约、
tool_calls 计数及观察原子保存。不存在伪造 assistant.tool_calls、Action 或 tool 消息。

该 Core Tool 的完整注册绑定与根字段规则固定，外发的根值已经通过来源披露和 4 KiB 限制。
Provider 通过真正的 tool_call_id 看到证据，工具内容不提升为 user/system。
模型要求其他工具、重复读取或输出未经执行的假观察均不能发布摘要；已有只读结果恢复时直接复用。
若该只读行动曾 UNKNOWN 后被人工对账，持久结果也必须逐项等于冻结工具输入；替换数据会在下一次模型预约前
SummaryEvidenceChanged，不能借 Core 工具的公开绑定扩大对账结果权限，原审计记录不删除或改写。
普通 DAG 仍传递 Artifact 输出，但默认 Durable Agent 会排除摘要标记依赖，防止 v1 自动消费。
应用若刻意把文本拼入 Prompt，仍须自行承担独立消费授权；本功能不保证任意自定义 Handler 的数据流。

## 模型尝试与原子发布

每个摘要 Step 有自己的 AgentSession、ModelCall、ModelAttempt、Action 和预算。默认两轮：
第一轮请求真实只读证据，第二轮返回 JSON；同一来源 Agent 的预算不增加。
SummaryInputManifest 绑定目标 Step、稳定 request_id、策略、source snapshot、证据引用与输入 digest/大小。
SHA-256 使用有限、sort_keys=True、ensure_ascii=False、紧凑 JSON 的 UTF-8；摘要不是加密或事实认证。

ProjectionManifest 和 ModelRequestContract 增加可选 summary_input_digest；未使用时省略，旧 v1/v2/v3
Checkpoint/Egress 字节保持。整个 Contract 继续冻结 Provider/model/选项/实际上下文和工具 Schema。
SQLStore 在真实 Run/Step 锁下重新验证来源和输入，再申请 Permit、保存 Attempt，提交后才允许外部请求。
源只读校验在持有同一 Run 锁时执行，不在已锁模型/行动后获取其他来源行锁，保留既有锁顺序。
数据库工作通过线程卸载；ContextBuilder、Projection、Checkpoint、Egress 均没有新模型调用。

模型只提交 SummaryDraft：schema_version、claims。每条声明含唯一 claim_id、非空有界 text、现存
reference_id、uncertainty（evidence_linked/inference/unknown）及 referenced/unattributed。
引用和状态证明能定位原记录，不证明声明真实或推理正确；拒绝 confidence 概率字段，分类不表示统计校准。
未知引用、重复声明、非法/重复键 JSON、额外执行/审批字段和超限输出拒绝，不降级为可信文本。

Store 在同一完成事务内再次解析实际 assistant 响应、核对真实只读 Action/结果与来源、引用、覆盖率，
由服务端生成不可变 SummaryArtifact，再与 Agent SUCCEEDED、Step succeeded、ArtifactRow 和 usage
确认一起提交。模型不能指定 generated_at、源身份、尝试、digest 或审批状态。输出完整 Artifact 也受
max_summary_bytes 限制；源 Agent/Artifact 不被修改，摘要失败可令本 Run 汇总失败，但来源 Step 保持成功。

Artifact 含 request_id、SourceSnapshot、策略/输入/请求契约/内容 digest、真实 attempt 引用、数据库生成
时间、声明、覆盖率、未覆盖引用及 generated_not_fact_verified 标记。覆盖率只计算本 Snapshot **选定引用**
中被声明引用的比例，不度量全文语义覆盖。源选择遗漏数量单独记录，未授权内容不被宣称已总结。
允许不确定声明必须显式开启，推断/缺失信息仍保留标记。verify_summary_artifact 用冻结输入、已成功尝试和
原响应只读核查恢复后的 Artifact；不能只凭一个结构合法 JSON 认证来源。

## 错误与恢复

| 窗口 | 持久事实与恢复 |
| --- | --- |
| 确认可恢复失败/429 | 原 Attempt FAILED，保留逻辑 model_call_id；数据库未来 available_at，释放租约；有界预算内下次预约新 attempt_index，不占 Worker 等待 |
| 协议/JSON/引用/覆盖率错误 | Summary Agent FAILED，已知 usage 正常记录；无摘要 Artifact，不自动整步重试模型 |
| Provider UNKNOWN / ReadTimeout | Attempt UNKNOWN、Agent fail-closed；不改写为可重试失败，不重发 |
| 模型预约提交后进程退出/确认丢失 | 持久 INFLIGHT 恢复为 UNKNOWN，不假设请求尚未发送 |
| 证据工具 INFLIGHT 后退出 | UNKNOWN + WAITING_RECONCILIATION；即使只读也遵守现有不自动重派规则 |
| 证据结果已提交后退出 | 复用 Action 结果，只生成尚未发生的最终模型尝试 |
| Artifact/结果事务失败 | 回滚确认、Step 与 Artifact；结果不确定时保守 UNKNOWN，不补造 usage 或第二份 Artifact |
| Artifact 提交成功但确认丢失 | 读取已提交终态；旧 token 不能再次完成，Artifact 不重复 |
| 协程取消/超时/业务取消 | 继承原 CancelledError 传播和最多五秒协程清理；只用原 lease/token；业务取消不能复活，不能强制终止已入驱动的同步线程 |
| 源 revision/定义/策略漂移 | 不切换冻结输入，fail-closed；所有权/冲突由 Worker 保守停止及原租约恢复，不通过改写终态/权限绕过 |

取消和 UNKNOWN 清理读取已持久冻结的用量策略，不要求漂移后的策略仍可派发；失效配置不能阻断取消。

“摘要失败不重试”指终态 FAILED 和未知结果；只有明确分类的确认可恢复模型失败允许上述有界调度。
新 Summary Step/来源版本可创建独立派生 Artifact，不能重置同一执行预算或覆盖旧摘要。

## 数据库、部署及下一阶段

复用已有 Step、AgentSession、消息/ModelAttempt JSON 与 Artifact 表，没有 0009，也不修改 0001–0008。
新数据纳入现有单项和累计 8 MB 限额、UNKNOWN/取消控制余量；无 pickle、隐藏账本或新 Scheduler。
部署须统一升级 Writer，旧程序无法解析新 JSON，不能混跑并假定旧程序会执行摘要权限门禁。
备份/恢复演练包含真实 Summary Source/Attempt/Artifact，以及旧 v1/v2/v3、审批/对账/延期事实。

离线 Mock/MockTransport 验证的是协议、权限和事务边界，不证明真实 LLM 抗 Prompt Injection、完整性或准确性。
公开证据仍可能包含恶意指令；模型可遗漏、幻觉或错误归因。public 不是 PII 检测，ToolBinding 不是实现签名。
字节限制不是精确 Token 或费用上限；usage 只是 provider-reported/unavailable，不是 exactly-once 账单。
真实 Provider、跨宿主、生产长时负载及 HA/PITR 仍须单独授权验收。

下一阶段消费 Summary 必须显式授权读取与披露，核对来源/策略/契约/版本及不确定性，使用非特权派生数据角色，
并保留回查原证据能力。不能让摘要生成工具调用、审批、对账或覆盖权威历史；消费策略漂移、取消、UNKNOWN、
预算和压缩语义损失需要独立回归，不能默认把已有摘要当 system 指令或完整可信 Memory。
