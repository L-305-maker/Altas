# Summary-Aware Context Consumption v1

[Token Window](token-window-conformance.md) 在消费授权、Egress 和投影完成后计量实际冻结消息及完整工具 Schema。
消费视图、声明数与 Artifact 字节数均不替代完整请求 Token 数；独立消费模型请求仍须预算、用量治理和 Admission。
有 Profile 的 v2 Contract 同时保留 summary_consumption_digest；旧 v1 身份不变。

Summary 是模型生成的派生 Artifact，不是权威历史、事实认证、操作员声明或执行许可。
生成授权和消费授权分别检查。默认 `disabled` 保留原模型输入；没有隐藏模型请求、自动摘要替换、
Memory、RAG 或 Planner。只有明确配置的 Durable Consumer Step 才注册真实只读工具。

## 配置与授权

```json
{
  "id": "consumer",
  "handler": "agent",
  "depends_on": ["summary"],
  "parameters": {
    "agent": {
      "mode": "durable_v1",
      "summary_consumption": {
        "mode": "verified_tool",
        "consumption_version": 1,
        "source_summary_step": "summary",
        "max_consumed_claims": 8,
        "max_consumption_bytes": 4096,
        "minimum_selected_evidence_coverage": 1.0,
        "allowed_uncertainty": ["evidence_linked"],
        "allow_claim_text": false,
        "require_referenced_claims": true,
        "allowed_summary_versions": [1]
      }
    }
  }
}
```

SummaryConsumptionPolicy frozen、extra=forbid；mode 默认 disabled，v1 版本严格为整数 1。
声明数量 1–32，视图 UTF-8 JSON 字节限额 256–4096，覆盖阈值为有限 [0,1]。
布尔和数值使用严格类型；uncertainty 非空且无重复；目前只支持真实 Summary v1。
策略只能来自已持久 Run 中的 Consumer StepSpec。Run.inputs、Prompt、依赖 JSON 和模型输出
不能选择其他 Run/Session/Artifact，也不能提高文本授权、预算或工具权限。

默认元数据视图不含 claim.text，并用 Core 的 `claim-<SHA256>` 替换模型自选 claim_id，
避免生成标识夹带正文。保留声明 digest、真实引用、uncertainty 和归因分类。
显式 allow_claim_text=true 才保留有界原文本及原声明标识。public 分类不是 PII/Secret 检测；
哈希不是加密，不提供低熵内容的保密性。

## 真实持久来源

SQLAgentStore.summary_consumption(task) 复用 Worker Engine，在有效 Run/Step/token/数据库时钟租约下：

1. 从真实 Consumer StepSpec 验证 durable_v1 和明确 depends_on；按 key 解析同 Run StepRow UUID。
2. 检查来源 handler=durable_summary、Step succeeded、唯一真实 Artifact、所属 Run、
   Artifact digest 及其与 Step.output 一致性。Task.dependencies 的内容还须匹配持久输出；它自身不能授权。
3. 恢复已 succeeded 的 Summary AgentSession，验证完整 assistant/tool/Action 历史与冻结 SummaryInput。
4. 重读原 Source Step、AgentSession、Revision、完整历史/StepSpec 与历史 ToolBinding，重新生成授权来源，
   必须精确等于冻结 Snapshot/Manifest。UNKNOWN/未完成或无法定位的来源 fail-closed。
5. 调用 verify_summary_artifact：真实成功 Attempt、请求契约、输入身份、实际已提交 assistant 输出、
   真实证据读取结果、声明与引用均须一致。合法 JSON/assurance/content digest 单独不能认证 Artifact。

来源读取不接受外部身份参数。所有 Writer 先锁同一 Run，稳定来源采用无后置行锁读取，
保留 Run → Step → Session → ModelCall → Attempt → Action 的既有锁约束，不在模型事务内调用外部工具。
摘要、原消息、Action、审批、对账、用量与 Artifact 不被消费过程覆盖或修补。

## 确定性视图与真实工具

按照原声明顺序过滤 allowed_uncertainty、引用要求，取最多 max_consumed_claims 的前缀。
没有合法声明或整个规范视图超过字节限额时拒绝；不截断文字、不偷偷缩减到另一个选择。
minimum_selected_evidence_coverage 检查过滤后所选声明实际引用的数量比例，分母仍是原 Snapshot
选定引用。原摘要 evidence_coverage 另保存在 summary_evidence_coverage；省略声明不能沿用原来的高覆盖率。
两者都不是全文事实或语义覆盖率。

VerifiedSummaryView 保存 Artifact digest/request_id/version、SourceSnapshot digest、消费策略 digest、
selected_claims、omitted_claim_count、selected_evidence_coverage、原 summary_evidence_coverage、source_completeness、来源遗漏与
所选声明引用的截断证据数量、assurance=generated_not_fact_verified 和 view_digest。
省略的声明不会产生新证据；unknown/inference 不升级为 evidence_linked，未归因不变成已引用。
即使覆盖率为 1.0，completeness 仍为 selected_evidence_only。

同一 Artifact/Policy 生成相同规范 UTF-8 JSON/hash。Artifact digest 沿用 Store.json_bytes 的排序、
有限值、非 ASCII 转义 JSON；policy/view digest 使用排序、有限值、紧凑 JSON 的 UTF-8 SHA-256。
初始化消息附加字段持久保存冻结消费输入，
不作为 user.content 回放。`read_verified_summary({})` 仅在该任务的私有 ToolRuntime 副本注册，
真实 version=1、策略 SHA-256 纳入描述与 ToolBinding digest，无任意身份参数，无外部写操作。
正常走白名单/Schema、Action 预约、tool_calls 硬预算、租约、执行及原子观察持久化。
模型可以不读取工具并直接回答；Core 不伪造调用或观察，也不为“完成消费”创建隐藏模型请求。
任务私有注册保留原 Runtime/Runner 的授权与输出校验扩展，只复制工具字典；不修改共享白名单，
也不以新基类实例替换部署已有的拒绝策略。

证据始终作为 tool 角色数据；system/user 仍为原固定消息与目标。模型生成的恶意指令不具有系统权限。
消费不会授权其他工具、消耗/生成行动审批或自动对账；原本需审批的工具仍须绑定该具体行动。

## Egress、请求与恢复身份

完整 Consumer 历史验证 → 真实 Artifact/来源及冻结视图重验 → 真实工具读取 → ContextBuilder
→ Projection/Egress → UTF-8 字节与 Capability/Token Preflight → 硬调用预算 → Observed Usage
→ Provider Admission → 原子 Attempt/Manifest/Contract 预约 → 所有权重读 → Provider HTTP。
首次让模型选择读取前也已验证并冻结消费身份；消费仍使用 Consumer 原有模型和工具预算。

没有显式 Egress 的旧模式保留原其他工具行为；显式 Egress 不会自动扩大。
有显式 Egress 时须授权该真实 Core Binding 的根视图 approved_excerpt/public，且字节限额足够。
可在可信注册代码通过 `consumption_tool(policy)` 和 runtime.binding 构造规则。已有 metadata_only
或不匹配规则 fail-closed；v3 Checkpoint 仍须与 Egress 披露策略相同。其他工具继续按其原规则处理。

ContextProjectionManifest 与 ModelRequestContract 增加可选 summary_consumption_digest，绑定 view_digest；
实际工具 Schema/Binding 描述仍计入 schema/context 身份。字段为 None 时完全省略，旧 v1/v2/v3、
Summary v1、Egress 及请求身份不回填、不改写。新增配置不传给 AgentBudget。
已确认失败的重试复用 model_call_id；Artifact、Policy、所选声明、视图、模型与工具 Schema 均不得漂移。
不一致预约前停止，不能换输入沿用旧逻辑调用。

| 窗口 | 保守行为 |
| --- | --- |
| 缺依赖、缺 Artifact/Attempt、失败来源、内容/策略/Revision 损坏 | 不发 Provider 请求；StateConflict 或安全校验错误，停止当前执行，必要时由原租约机制回收，不猜测/再生成来源 |
| 已确认可重试模型失败 | 原有有界延期，不重置预算/视图，不消耗虚假 Step 失败次数 |
| 模型预约后崩溃/确认丢失 | INFLIGHT 变 UNKNOWN，fail-closed，不重复请求 |
| 消费工具 INFLIGHT 后崩溃 | UNKNOWN + WAITING_RECONCILIATION，即使只读也不自动重派 |
| 工具结果提交成功后崩溃 | 原 Action、结果及身份复用，仅继续尚未发生的模型调用 |
| UNKNOWN 经人工对账 | 成功读取结果必须精确等于冻结视图；不能借公开 Core Binding 替换/夹带新数据 |
| 完成提交后确认丢失 | 读取已提交事实，不重复确认 Attempt/Artifact |
| 业务取消、旧 token、CAS 或 DB 不确定 | 原 fencing/终态与取消规则继续生效；不使用新 token 补交迟到结果 |

不为来源不一致放宽终态，也不强制终止已经进入同步数据库驱动的线程；协程取消仍向上传播，
清理最多等五秒。8 MB Session 配额包含冻结视图及新增 Manifest，控制记录预留不变，满额时保留取消/UNKNOWN 清理空间。

## 发布和边界

无新表/迁移，保留 0001–0008；复用初始消息 JSON、Action/Observation、Attempt JSON 与 Artifact。
所有 Writer 统一升级后再启用，不能让旧 Writer 默默忽略新权限或 JSON 字段。备份恢复须比较完整持久事实，
并核对冻结视图与 Contract；恢复后不得重派 UNKNOWN 或已完成读取。

测试使用合成恶意文本与 MockProvider，只证明角色、权限、协议和事务边界，不证明真实 LLM 的准确性、
完整性或抗注入效果。生成文本可能幻觉；引用正确不等于结论真实；字节限额不是 Token 窗口或费用上限。
没有已确认的真实费用 exactly-once 结算。真实 Provider、跨宿主网络故障、生产长时负载和 HA/PITR 仍需独立验收。

下一阶段 Token Window 应由可信 Provider 能力与可验证计数器执行 Preflight，保留 usage 未知语义，
不能以摘要长度/覆盖率估算冒充硬上限。生产 RC 需专用 Secret、显式付费授权、连接与锁超时、租约/心跳、
跨主机故障和恢复演练；摘要消费权限与业务行动审批始终独立。
