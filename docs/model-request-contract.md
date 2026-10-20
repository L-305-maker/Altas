# Immutable Model Request Contract & Capability Preflight v1

本功能固定一次模型尝试的应用请求身份，不证明供应商的模型权重、实际账单或网络副作用 exactly-once。
完整历史、Context Projection、调用预算、Observed Usage、Provider Admission、CAS/fencing 和取消规则均保留。

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

DeepSeek v1 保持默认 4096 输出上限，可显式降低；保守适配器上限仍为 4096，包括旧接口，不宣称这是供应商最大容量。
Durable 静态识别 deepseek-chat、deepseek-reasoner 兼容名及 deepseek-flash、deepseek-v4-pro。
reasoner 兼容配置保守禁用工具、JSON 和采样参数；未知模型拒绝，SSE 均不支持。
当前公开请求规范参见 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。
这些是版本化适配器配置，旧别名可用性、供应商别名映射及真实 API 行为本轮未付费实测；不会自动切换到另一个模型。

Contract provider_key 表示逻辑 Provider 身份；Admission policy.provider_key 是共享限流池标签，二者不要求同名。
部署者必须让所有 Worker 使用一致的身份和合适的限流池。Key 轮换不改变 Contract；跨账户轮换须调整身份，
Core 不通过读取 API Key 推断账户，也不把 Key 的摘要作为持久身份。

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

验证覆盖：MockTransport 冻结网络请求、部署漂移、窗口边界、真正 Worker/Store、配额/软用量门控、旧 Schema 只读审计、
迁移全量事实、独立 PostgreSQL CAS、崩溃、取消、三进程 Soak、备份恢复及 Wheel。实际结果见 RC 验收记录。
仍需目标环境 Provider、跨宿主及运维演练；本轮不发送任何真实付费请求。
