# Provider Context Egress Control v1

本功能显式控制 **Tool Observation Result** 的 Provider 外发视图。完整消息、原始结果、审批、对账、
Action ID、幂等键和用量账本不变。它不执行模型摘要，也不构成所有输入的 DLP。

## 默认行为与兼容矩阵

| Projection | 未配置 provider_context_egress | metadata_only | bound_disclosure |
| --- | --- | --- | --- |
| full | 原完整工具结果回放 | 只发送工具状态/安全错误码/历史绑定摘要，结果仅含空 fields | 只发送精确绑定且 public/approved_excerpt 的字段 |
| recent_complete_rounds | 原完整结果，按完整轮次移除旧后缀以外历史 | 同上，再选择完整后缀 | 同上，再选择完整后缀 |
| evidence_anchored_recent_rounds + v3 | 保持已有 v3 披露与 Manifest | 仅当 v3 disclosure 与对应的空规则策略完全相同时接受 | 仅当 v3 disclosure 的规则、顺序和字节限制完全相同时接受 |
| 历史 v1/v2 checkpoint | 历史字节读取不变；v1/公开 v2 仍禁止新预约，metadata v2 原身份恢复 | 不将新权限附到旧请求，拒绝组合 | 拒绝组合 |

省略配置或 null 表示历史兼容行为，概念上为 legacy_full。不能显式设置 disclosure_mode=legacy_full。
此版本不改变既有 Run 默认值，也不替已有生产 Run 自动添加安全策略。新 Workflow 应显式选择安全模式。
旧 AgentRunner/普通 Handler 不受此功能治理。配置只从持久化 StepSpec.parameters.agent 读取；没有第二套
热更新部署覆盖策略。部署者可在可信组合入口生成 StepSpec，不能从 inputs、Prompt 或工具输出生成授权。

## 安全配置

```json
{
  "mode": "durable_v1",
  "max_context_bytes": 1000000,
  "context_projection": {"mode": "full"},
  "provider_context_egress": {
    "policy_version": 1,
    "disclosure_mode": "metadata_only",
    "max_disclosure_bytes": 256,
    "rules": []
  }
}
```

ProviderContextEgressPolicy 冻结、拒绝额外字段，版本严格为整数 1；规则最多 32 条。
metadata_only 不允许包含规则。bound_disclosure 使用 Evidence Disclosure v3 同一类型与纯决策函数。
只有完整历史 ToolBinding 相等、binding.matches(action)、精确路径、classification=public 且
mode=approved_excerpt 才允许原文；DENIED 始终只有元数据。FAILED/SUCCEEDED 保持真实状态。
缺绑定、同名不同版本、policy digest 或副作用/审批属性不匹配，均不能获得原文权限。

可信注册入口可构造显式字段授权（不将 API Key 放入规则）：

```python
binding = runtime.binding("read")
egress = {
    "policy_version": 1,
    "disclosure_mode": "bound_disclosure",
    "max_disclosure_bytes": 256,
    "rules": [{
        "tool_name": "read",
        "binding": binding.model_dump(mode="json"),
        "result_path": ["public_value"],
        "mode": "approved_excerpt",
        "classification": "public",
    }],
}
```

恢复时比较的是 StoredAction.tool_binding，不能用当前 registry 替换旧绑定。规则中的绑定是部署者
选择要授权的身份；当前注册版本升级不会追授历史私密结果的权限。规则允许明确的空路径表示整个 result，
此授权范围很大，应优先使用具体字段。Sensitive/unknown 规则必须 metadata_only，不会自动识别内容中的秘密。

plain full/recent 每个字段按有限紧凑 JSON 的 UTF-8 字节计量（包括字符串引号/转义）；超过
max_disclosure_bytes 返回 AgentToolDisclosureLimit，不截断字段、不放宽策略、不预约/申请 Permit/HTTP。
Checkpoint v3 的既有摘录截断和 per-field 限额保持原语义；组合配置仍由 v3 唯一渲染。
两类结果均受最终 max_context_bytes 和 Session 8 MB 存储限额约束。

## 唯一披露决策与事务

完整 Session → ContextBuilder 验证全部历史/映射并拒绝 UNKNOWN/PROPOSED/INFLIGHT → 按历史绑定披露
→ 完整轮次投影/协议校验 → 精确字节上限 → 调用硬预算 → Observed Usage → Provider Admission
→ Snapshot/Attempt/Manifest/Contract 原子预约 → 重读所有权/Permit → Provider HTTP。

Egress 与 v3 checkpoint 同时启用时，不合并白名单。只有 EvidenceDisclosurePolicy 完全相等才接受，
复用原 v3 renderer 和原 checkpoint Manifest；否则 AgentContextEgressPolicyConflict 在预约前安全终止。
例如 metadata_only 不能与 public checkpoint 组合，不能先脱敏后由 checkpoint 再泄露。
即使两份规则语义可能等价，顺序或限额不同也不猜测，必须由部署者提供一致配置。

SQLAgentStore 在 Run→Step→Session 锁下，从数据库原始历史与真实 StepSpec 重算新视图；调用者删掉
Manifest、伪造策略摘要或 Task.spec 均不能跳过。既有 CAS、Lease、fencing、Action 授权和取消规则不变。
清理/对账不进行披露或外部模型调用；UNKNOWN 即使在将省略的轮次中也先阻断。

## 请求身份与历史

ContextProjectionManifest 新增可选 egress：egress_version、policy_digest、source_actions_digest、
observations_digest。摘要使用 sort_keys=True、ensure_ascii=False、allow_nan=False、紧凑分隔符的
UTF-8 JSON SHA-256。source_actions_digest 绑定完整持久 Action 事实；observations_digest 绑定窗口选择前
的所有有效观察，最终 projected_context_digest 绑定实际选中消息及工具 Schema。
ModelRequestContract.context_egress_digest 绑定整个 Egress Manifest，再计入已有 request_contract_digest。
Manifest/Contract 不保存原始工具结果、Prompt 或凭据。
摘要用于一致性核查，不是加密或自动脱敏。

这些字段只扩展 0007/0008 已有 JSON；没有 0009 迁移。未启用时省略新增键，v1/v2/v3 旧字节与摘要不变。
回归夹具来自真实旧基线：v1 8219b93、v2 918ae92；v3_b0f4a7a 通过只读 git archive 加载审核基线的
源码与合成历史，在隔离临时目录捕获 Manifest、Contract、wire context；不由新实现回填预期值。

同一逻辑 model_call_id 已确认失败后，下一次真正预约才增加 model_attempts。恢复时 Manifest 和
Contract 必须完全相等；即使新策略产生相同文本，只要身份变化也停止。UNKNOWN 不自动重发。
COMMIT 确认丢失时，只读取数据库 Attempt/Manifest，不能为“核实”而重发。pg_dump/restore 逐事实核查
新身份与旧 v1/v2 事实；旧二进制无法识别新键，部署须统一升级 Writer，不能混跑后假定旧 Writer 会过滤。

## 容量与有限工作量

原审计结果单项/累计限制不变；新固定大小摘要与 Contract 字段计入 Session 配额。派发前原有控制余量仍
为 UNKNOWN/取消/审批/对账保留，不能为了保存新身份跳过清理。接近配额的独立事务测试验证取消仍可落库。
工具实际返回结果超过存储上限时，沿用 UNKNOWN + WAITING_RECONCILIATION；不截短为成功结果，
不跳过观察进入下一轮模型，也不重新执行原 Action。

非 checkpoint 的显式 Egress 窗口将每条过滤后消息序列化一次，用各完整轮次的字节长度累计选择最小省略数，
只对最终视图再完整计量；不会针对每个候选窗口反复序列化巨大的原始结果。有限长历史测试断言完整计量次数
恒为两次，而非用机器绝对耗时声明生产 SLO。原 ContextBuilder 与完整 source/context digest 仍读取整个有界
Session；checkpoint 保留既有有限轮次搜索，不承诺无限历史、常数内存或生产吞吐。

## 已证明范围与剩余路径

- 工具数据只保留在真实 tool_call_id 对应的 tool 消息，不提升为 user/system；不造 Action、审批或观察。
- 原始 user goal/依赖并不自动脱敏；assistant 文本、reasoning_content、assistant tool_call arguments
  也不属于本轮结果过滤保护。公开字段可包含恶意文本，消息角色并不证明真实模型抵御注入。
- ToolBinding digest 不签名 Python 函数或远端服务实现，行为/敏感性变更必须更新版本。public 不代表经过
  PII/Secret 检测；历史绑定一致不证明结果的现实真实性。
- 字节上限不是供应商精确 Token 窗口或费用上限，外部副作用/费用没有 exactly-once 保证。
- 本轮测试使用 MockProvider/MockTransport；真实 Provider、跨宿主和生产长时负载仍需独立授权验收。

下一阶段 LLM-assisted Summarization 必须先建立摘要输入自身的 Egress 授权、不可变摘要请求/Attempt/usage、
来源与不确定性引用、派生版本/摘要 digest、非特权承载及对原文权限的交集规则。摘要不得恢复或扩大已省略/未授权
内容，不能作为新的系统指令或事实认证；需独立故障、预算、UNKNOWN 与注入测试。本轮不调用模型生成摘要。
