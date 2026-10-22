# Immutable Model Request Contract & Capability Preflight v1 / v2

[Token Window Preflight](token-window-conformance.md) 增加不可变 TokenCounterProfile 与显式 RequestContract v2。
完整 Profile 绑定模型、模板/资产/证据及能力；只计最终冻结 wire 请求一次，窗口检查先于 Permit/Attempt。
无 Profile 继续原 v1 序列化及摘要，旧历史只读，不回填。真实 DeepSeek 尚无已验证 Counter，strict 仍拒绝。

[Summary Consumption](summary-consumption.md) 的可选 summary_consumption_digest 原子绑定冻结视图；
None 时省略，旧身份不变。重试不能更换 Artifact、Policy、声明选择或工具绑定。

[Durable Summary](durable-summarization.md) 的 Projection/Contract 使用可选 summary_input_digest 固定来源、
策略及证据身份；初始只读工具轮次和最终 JSON 轮次共享该身份，各有自己的 Attempt/Contract/usage。
新字段参与契约摘要与重试连续性校验；普通历史请求完全省略此键。Store 重新验证来源后才发放 Permit，
不在 Capability/Token Preflight 内隐藏摘要请求，也不把生成结论当成工具执行授权。

显式 [Provider Context Egress](provider-context-egress.md) 在 ProjectionManifest 中保存可选 egress 身份，
Contract.context_egress_digest 绑定整个该 Manifest 并参与 request_contract_digest；未启用时完全省略新键。
模型重试核对整个投影/Contract，因此策略变化即使没有改变文本也 fail-closed。使用既有 JSON，无新迁移；
v1/v2/v3 审核夹具的历史字节可重建。新增身份不包含正文，不改变已提交旧请求摘要。

本功能固定一次模型尝试的应用请求身份，不证明供应商的模型权重、实际账单或网络副作用 exactly-once。
完整历史、Context Projection、调用预算、Observed Usage、Provider Admission、CAS/fencing 和取消规则均保留。

[Evidence Checkpoint](evidence-checkpoint.md) 显式模式在 ProjectionManifest 的可选 checkpoint 保存来源引用与身份。
RequestContract 的 context_projection_digest 仍保护实际发送的全部消息及 Schema；v3 工具证据只在真实 tool 内承载，旧 v1 只读保留原字节。
Store 在 Permit 前从原始历史重建来源、摘录、Manifest 和请求视图；重试要求整个 Manifest/Contract 相同。
未启用时省略新增 JSON 键，历史契约及摘要不变；无新迁移，不回填旧证据或改写 Usage/Permit。
摘要证明一致性，不认证来源内容的现实真实性，也不能授予工具权限。

## 可信配置和不可变派发

配置只来自部署 Settings / 适配器构造参数。Workflow 普通 inputs、Prompt、工具结果和模型输出不能更换配置。
`AGENTFLOW_MODEL_PROVIDER_KEY` 是非敏感的稳定 Provider/账户标签，默认 mock/deepseek；它不是 API Key。
`AGENTFLOW_MODEL_REQUEST` 是有类型、冻结、拒绝额外字段的 JSON，例如：

```json
{
  "max_output_tokens": 1024,
  "stream": false,
  "tools_enabled": true,
  "strict_token_window": false,
  "parameters": {"temperature": 0.2, "top_p": null, "response_format": "text"}
}
```

允许参数只有 temperature [0,2]、top_p (0,1]、response_format text/json_object。
值必须有限并满足适配器声明的能力，不能传入任意请求字典。输出上限是正整数；v1 只消费完整 JSON，stream 只能为 false。
这些字段不进入 AgentBudget，也不改变 max_resource_units=0 的现有语义。

新增 `RequestModelProvider.prepare_request(messages, tools, manifest)`，返回 PreparedModelRequest：
ModelRequest 将上下文保存为 repr 隐藏的不可变 JSON 字符串，Contract 冻结模型及参数；execute 回调只使用预约前捕获的派发信息。
DeepSeek 捕获 endpoint、Key、transport 和实际请求体来源，提交后不会再读取可变 Provider 配置。
Key 和 endpoint 原文不进入 Contract；包含 query、fragment 或用户凭据的 Durable endpoint 被拒绝。
每次生成实际 payload 都复核消息和 Schema 摘要。敏感上下文及 execute 回调不会出现在 prepared 对象 repr 中。

旧 complete()/respond() 保留；旧 AgentRunner 无需重写。Durable 不回退到无契约的 respond-only 适配器，
而是以 ModelRequestContractUnavailable 在预约前停止。自定义 Durable Provider 必须增量实现 prepare_request，
并确保 execute 实际使用冻结的数据；不能简单回调一个会重新读取可变网络配置的方法。
Mock 的 execute 转发原 respond 测试行为，同时记录冻结 Contract；它不是实际模型。
付费 Smoke 的一次请求包装器同步转发此接口，原三重授权、一次请求和超时门控不变，本轮只用 Mock 验证。

## Contract、能力与版本

ModelRequestContract 包括 provider_key、model、adapter_version、request_contract_version、max_output_tokens、
stream、tools_enabled、strict_token_window、类型化 parameters、ProviderCapabilities、endpoint_fingerprint、
context_projection_digest、tool_schema_digest、TokenCount 以及 request_contract_digest。
ProviderCapabilities 绑定 Provider/model、capability_version、工具/流式/JSON 支持、输出上限、可用参数，以及可选窗口和计数方法。
能力来自可信适配器定义，不从模型响应或自然语言推断。

摘要统一使用 sorted-key、紧凑、ensure_ascii=False、allow_nan=False JSON 的 UTF-8 字节和 SHA-256，
不做 Unicode 语义归一化。request_contract_digest 对除自身外的全部持久字段计算，读库时重新验证。
上下文和 Schema 摘要沿用 Projection Manifest，Store 会从锁定完整历史重新计算并核对两份身份及能力。
只有指纹的 endpoint 也会影响请求身份；摘要不是加密或签名，不应把任何 Secret 填进身份标签或模型名。

DeepSeek 仍保持默认及适配器最大输出 4096，可显式降低，包括旧接口；这是应用侧保守限制，不是供应商最大容量。
Settings、Compose 和 .env.example 的新部署模型默认改为 deepseek-flash；provider 仍默认离线 mock。
已经显式设置 AGENTFLOW_MODEL 的部署不会被环境默认值覆盖。下列判断核实于 **2026-10-20**：

| 名称 | 生命周期分类 | Core 策略 |
| --- | --- | --- |
| deepseek-flash | supported；官方当前规范名称 | 允许文本 Chat、JSON、函数工具；新部署默认 |
| deepseek-v4-pro | supported；官方仍提供服务 | 同上；必须显式选择 |
| deepseek-chat / deepseek-reasoner | legacy；官方公告于 2026-07-24 停用 | 新请求在预约及 HTTP 前拒绝；不自动改名 |
| deepseek-v4-flash / deepseek-v4-flash-vision-exp | legacy；原模型退役，官方说明旧名称仍路由到新 Flash | Core 拒绝别名，避免把供应商重定向当作已固定的原模型 |
| 其他名称 | unsupported / unknown | 拒绝，不根据字符串推测能力 |

证据：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、
[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)、[Change Log](https://api-docs.deepseek.com/updates/)。
本次核查时，价格页将规范名称分别对应到 Flash-1017 和 Pro-0813；文档中的供应商版本不是 Core 固定权重的证明。
公告停用和旧名称路由是文档事实，本轮没有发送付费请求来实测这些 endpoint 的可用性。

Durable 旧别名使用 ModelDeprecated，未知名称使用 ModelCapabilityUnsupported；错误中不包含配置原文。
旧 complete()/respond() 在同一能力检查失败时使用既有 ModelConfigurationError，保留 ModelError 异常类别兼容。
历史 Contract 仍能验证、读取及备份，不调用生命周期白名单去改写档案。
规范模型现有的请求序列化、deepseek.chat.v1 和 deepseek.adapter.1 标识不变；v1.1 是兼容修复版本，
没有更改这些已持久字段的含义，因此原规范模型可在契约完全相同时继续确认失败重试。

Contract provider_key 表示逻辑 Provider 身份；Admission policy.provider_key 是共享限流池标签，二者不要求同名。
部署者必须让所有 Worker 使用一致的身份和合适的限流池。Key 轮换不改变 Contract；跨账户轮换须调整身份，
Core 不通过读取 API Key 推断账户，也不把 Key 的摘要作为持久身份。

## 两类 Provider 接口的请求语义

prepare_request 与旧 complete()/respond() 共享纯 serialize_request，按同一可信配置发送 model、max_tokens、
stream=false、response_format 以及显式配置的 temperature/top_p；messages、reasoning_content、工具 Schema 和调用 ID 原样传递。
新旧路径均检查生命周期、能力和 endpoint；DeepSeek 没有可信计数器，开启 strict_token_window 时两类路径都拒绝。
不添加任意参数字典、隐藏重试或由用户内容控制的选项。新的响应格式/采样配置现在也对旧接口生效，这是 v1.1 的行为修正。
手工构造的 context_json 也只能包含 messages/tools；共享序列化拒绝其他顶层键，防止通过上下文字典夹带契约外选项。

| 接口 | 请求构造 | 返回值与持久化边界 |
| --- | --- | --- |
| prepare_request | 预约前冻结上下文、参数、endpoint/凭据引用；执行时核对摘要 | ModelResponse；Durable 在 HTTP 前完成 Manifest/Contract/Permit/Attempt 原子预约及预算、CAS/fencing 检查 |
| respond | 每次调用入口捕获当时可信配置，使用同一纯序列化函数 | ModelResponse；接口本身不持久化、不申请 Permit，也不承诺跨调用恢复 |
| complete | 与 respond 共用请求路径，旧签名不变 | 原 assistant 字典；保留旧字段/角色校验语义，不把它描述为 Durable 协议 |

共享序列化保证同样显式选项的请求字段一致，不保证供应商采用采样参数或两次响应相同。
官方 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 当前默认开启 thinking：temperature 会被忽略，
Flash 的 top_p 只在 thinking 生效且低于 0.95 时会钳到 0.95；Pro 的 top_p 在 thinking 被忽略，非 thinking 才生效。
本版不暴露 thinking/reasoning_effort 控制，遵循供应商默认；不能把 wire 中的参数误称为已生效的采样保证。
工具模式需要回传所保留完整轮次的 reasoning_content；现有 ContextBuilder 与合规测试保留该字段，不将其提升为 system。
thinking、模型别名/权重及服务端默认值可能变化，Contract 固定显式请求身份，不固定供应商实现。

## Session、重试及恢复

同一逻辑调用的确认失败重试，model_call_id 不变、attempt_index 增加；Manifest 和完整 Contract 必须相同。
model/provider/adapter/能力版本/参数/输出限制/endpoint 或可信计数结果漂移，以 ModelRequestContractChanged 停止，
不预约新 Attempt、不增加预算、不取得新 Permit。Projection 漂移仍使用 AgentContextProjectionChanged。
旧失败尝试有 Manifest、没有 Contract 时使用 ModelRequestContractUnavailable，不能假造旧模型配置。
更旧记录连 Manifest 也缺失时，Runner 先以 AgentContextProjectionChanged 停止；不会继续猜测请求身份。

Session 从**第一条已记录 Contract**开始固定配置。后续新逻辑调用可使用新上下文与工具 Schema，
但其 profile_digest 必须相同；它仅排除上下文/Schema 摘要、单请求 TokenCount 和总摘要。
跨轮配置漂移以 ModelSessionConfigurationChanged 停止。旧成功调用缺失的配置保持 None，不追溯宣称它与新绑定相同。

ModelAttempt、Manifest、Contract、Snapshot revision、ModelCall 和 Permit 在原事务中原子预约。
Store 检查模型/能力/投影绑定和 Session 连续性后才申请 Permit，结果确认不写入或覆盖 Contract。
旧直接 Store 客户端在尚无已绑定 Contract 的非重试路径可继续省略它；新 DurableRunner 每次都提供。
任何失败重试都必须提供可验证的旧、新 Contract，不能通过省略字段继续未知身份的调用。
绑定后不能通过省略字段回到旧路径。所有权、数据库时钟、CAS 及锁顺序继续由 Store 保证。
派发前重新读取原租约和预约身份，不使用新 token 伪造所有权。

COMMIT 确认丢失后依赖持久事实恢复。MODEL_INFLIGHT 结果不确定时仍记录 UNKNOWN/fail-closed，
不能为了补全身份或用量重新请求。工具 UNKNOWN 仍必须人工对账；CancelledError 与最多五秒清理等待不变。

## Token 窗口的保证等级

max_context_bytes 仍精确限制 Provider 消息 + Schema 的紧凑 UTF-8 JSON，既不替代也不近似 Token 数。
TokenCount 明确区分 unknown、exact、upper_bound；未知保持 None，不能写零或使用 bytes/4。
可信 TokenCounter 接收完整有效请求 JSON，必须针对该 model、适配器帧格式、工具及输出格式等参数验证，
只返回已验证输入计数或可证明上界，不把生成的输出上限当作输入。
计数方法标识和保证等级必须与能力声明相同；只在声明中写一个方法名并不能通过严格检查。

有可信计数且有窗口时检查 input_tokens + max_output_tokens <= context_window_tokens（边界相等允许）。
strict_token_window=true 但计数未知或窗口未知，分别以 ModelTokenCountUnavailable / ModelTokenWindowUnavailable 拒绝。
已知超限使用 ModelTokenWindowExceeded；计数器失败或身份不符使用 ModelTokenCounterInvalid，均发生在预约前。
非严格模式允许未知计数，但不会把它描述为已验证窗口；已有可信计数时仍拒绝明确超限。

本版没有交付真实 DeepSeek tokenizer 或可证明上界，默认 TokenCount 为 unknown；其严格模式会安全拒绝。
测试用 exact/upper_bound Counter 是 Mock 的固定合成夹具，不是 DeepSeek 验收证据。
预检计数不写进 ModelUsage，不代表已发生用量、费用上限或供应商账单。

2026-10-20 调查结果：官方 [Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/) 提供离线演示包；
官方 [deepseek-recipe tokenizer 文档](https://github.com/deepseek-ai/deepseek-recipe/blob/main/docs/tokenizer.md)
也提供 V4/V4.1 tokenizer 与完整对话模板编码说明。因此并非没有可用离线资源，但本项目未安装、绑定或验证这些资源。
不能仅计数文本或照抄字符比例就声明完整请求 exact/upper_bound。

未来启用真实 Counter 前，验收必须记录：模型/服务版本、模板和 tokenizer 修订及资产 digest；完整消息、工具 Schema、
reasoning_content 与特殊 token 的帧规则；输入测量和输出保留量的区分；多字节/边界/多工具回归；与目标 API 的受控比对。
若只能估算或不能证明与当前服务模板一致，返回 unknown；没有完整证明时不能通过仅填写 method 名称开启严格窗口。
本轮只验证接口接受完整有效 payload、方法/保证等级匹配、未知不造零以及窗口边界，不交付真实 DeepSeek Token 计数保证。

## 0008 和升级前核查

0008_model_request_contract（down_revision=0007）只增加 agent_model_attempts.request_contract 可空 JSON 列，
不修改 0001–0007 或旧 Attempt、Action、审批、对账、Usage、Artifact。旧值保留 None，不回填。
Contract 计入 8 MB 会话限额；None 不增加旧会话的容量收费，预约继续预留 UNKNOWN/取消/结果控制空间。
downgrade 0007 删除请求身份，其他事实不变；再升级不会恢复已删除 Contract，旧失败调用将不能证明安全重试。

升级顺序：停止领取新任务及 Writer → 通过受保护连接运行只读核查 → 等待可确认请求结束并核查运行中租约
→ 逻辑备份 → 迁移 upgrade head → 再次核查 → 使用一致配置启动 Worker → 检查合法恢复与安全错误码。
不能用更改 token、手动改 FAILED/UNKNOWN 或补造摘要来“修复”审计报告。
旧 FAILED/READY_MODEL 缺身份应由操作者制定受控处置；需要重新执行时必须按原外部副作用和审批流程评估，不能自动重放。

```bash
uv run python scripts/audit_model_retry_upgrade.py --limit 1000 --offset 0
```

连接从 AGENTFLOW_DATABASE_URL 获取，建议 SELECT-only 账户。脚本直接识别 0006/0007/0008 的可用列，
只输出有界聚合：待重试、缺 Manifest/Contract、MODEL_INFLIGHT、不可证明恢复的数量和固定错误码。
缺 Manifest/Contract 及无法恢复计数针对待重试调用的最后一次尝试；已完成历史不作为待重试处置项计数。
报告无持久 ID、Prompt、参数、证据或凭据；不做迁移、恢复或补写。每页最多 10000 Session，offset 最大 1000000，
有 truncated/next_offset；各页独立快照，不声称更新中的多页是一次一致全库扫描。
PostgreSQL 使用 REPEATABLE READ / READ ONLY、5 秒 statement_timeout、2 秒 lock_timeout；SQLite query_only。
退出 0 表示此页未发现处置项，2 表示需要运维审核，1 表示核查未完成，不能当成健康报告。

v1.1 的报告 version=2 保留原计数，并增加：legacy_model_sessions、unsupported_model_sessions、
legacy_pending_retry_calls、unbound_request_sessions、terminal_historical_calls、operator_review_sessions。
模型分类仅从最后一次持久尝试中验证通过的 deepseek.chat.* Contract 识别；不能从最新 Settings 或供应商响应猜测旧身份。
unbound_request_sessions 统计此页有调用历史但末次 Contract 缺失的 Session，包含终态；其他缺失字段计数仍仅针对待重试。
terminal_historical_calls 是此页终态 Session 内逻辑调用数，不是尝试数。operator_review_sessions 按 Session 去重，
覆盖活跃旧/未知模型、执行中请求、无身份/无投影或契约损坏的待重试；已完成的旧模型档案合法，不因名称旧而要求重跑。
所有计数限定于当前页；调用总数在 SQL 内聚合，不无界加载历史记录。审计不读取凭据、不清理 Permit，也不改写任何契约。

验证覆盖：MockTransport 冻结网络请求、部署漂移、窗口边界、真正 Worker/Store、配额/软用量门控、旧 Schema 只读审计、
迁移全量事实、独立 PostgreSQL CAS、崩溃、取消、三进程 Soak、备份恢复及 Wheel。实际结果见 RC 验收记录。
仍需目标环境 Provider、跨宿主及运维演练；本轮不发送任何真实付费请求。

## Evidence v3 请求身份与 v1/v2 升级

v3 的完整 Manifest 绑定披露 policy_digest、disclosure_policy_digest、实际历史 ToolBinding 摘要、选择顺序、来源和承载后的实际消息。
RequestContract 不变，继续绑定该 Manifest 与 messages/tools 摘要；重试更换披露、pin、版本或 Schema 均拒绝。
Store 从锁定 Run 的真实 StepSpec 重建，不能通过替换 Task.spec 取得更高披露权限。
旧 v1/v2 Contract 和 Manifest 原样读取，不回填绑定/权限或更改摘要；新的 v1 预约明确拒绝。
v2 approved_excerpt 新预约也以 AgentEvidenceV2PublicReadOnly 拒绝；纯元数据按原 v2 身份重试，不能自动升级 v3。
只读升级审计按末次 Manifest 计 v1/v2/v3 会话，另按真实 StepSpec 计 v2 公开规则、活跃未绑定公开规则和阻断/处置数；
尚未预约的活跃 v2 公开会话也需要处置，不只检查已经失败的 ModelCall。
历史终态仍合法，不因发现旧 v1 就重新执行。报告无正文、token、供应商 request_id 或持久身份。
原始 audit 结果不因 Provider 视图过滤而删除。源 role、披露规则和运维处置详见
[Evidence Checkpoint](evidence-checkpoint.md)。
