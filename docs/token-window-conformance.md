# Verified Token Window Preflight v1

[RC Independent Acceptance v2](rc-independent-acceptance.md) 继续将 Token Conformance 独立列为
NOT_VERIFIED。有效签名、Provider Smoke、响应模型名称匹配或一次 reported usage 都不证明本地完整
Chat Request 计量与托管模型等价；仍需固定模型修订、模板/特殊 Token/工具计量资产及经独立审核的保证证明。
本次没有新增真实 DeepSeek exact/upper_bound Profile，也没有付费 Token 对照请求。

RC 发布判定见 [Production RC Staging](production-rc-staging.md)：UNKNOWN 不能启用 strict；
EXACT 需托管模型/完整 framing 等价证明；UPPER_BOUND 需覆盖声明范围的保守上界证明。
当前部署身份的真实 Counter Profile 清单为空，报告不会把离线向量、配置 digest 或一次回传 Usage
提升为真实 Token Conformance PASS。LIVE TOKEN CONFORMANCE NOT VERIFIED。

核实日期：**2026-10-22**。本阶段提供模型绑定 Profile、完整冻结请求预检与离线合规检查。
**尚无真实 DeepSeek 完整 Chat 请求的已验证 Counter；其 strict 模式继续 fail-closed。**
测试中的 synthetic Counter 只证明 Core 契约，不能证明真实供应商 Token 数或费用。

## 官方证据及未闭合部分

以下为只读公开资料核对，没有认证 API 调用、付费请求或外部代码执行：

| 来源 | 2026-10-22 核对结果 |
| --- | --- |
| [官方模型与定价](https://api-docs.deepseek.com/quick_start/pricing/) | `deepseek-flash` → `DeepSeek-V4.1-Flash-1017`；`deepseek-v4-pro` → `DeepSeek-V4-Pro-0813` |
| [官方模型目录协议](https://api-docs.deepseek.com/api/list-models/) | 示例中两者 `context_window=1048576`，`max_output_tokens=393216`；窗口同时包含输入与输出。未请求需认证的 `/models`，示例不是部署端实时发现结果 |
| [Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/) | 提供离线 demo；字符比例是近似值，不能作为预执行精确值或保守上界。该页面的 demo zip 本次读取返回 HTTP 错误，没有可验证的版本、许可或 Digest，不使用 |
| [Chat 请求协议](https://api-docs.deepseek.com/api/create-chat-completion/) | 需覆盖消息、工具调用及工具描述/Schema、输出格式等；API 文档不是服务端完整 Token framing 的等价证明 |
| [官方公开 V4 模型包](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/60d8d70770c6776ff598c94bb586a859a38244f1) | MIT；固定修订 `60d8d70770c6776ff598c94bb586a859a38244f1`；没有 Jinja chat_template，改由 `encoding/encoding_dsv4.py` 编码，含 thinking、工具及分隔符处理 |

公开 V4 包候选资产的**原始字节 SHA-256**（只核对、未安装或打包到运行时）：

| 资产 | SHA-256 |
| --- | --- |
| `tokenizer.json` | `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf` |
| `tokenizer_config.json` | `6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547` |
| `encoding/encoding_dsv4.py` | `bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0` |
| `encoding/README.md` | `605363e9e43ee91beba88ea96c7806ce6ecdb2924e481459c9d16e1526470c10` |
| `LICENSE` | `f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040` |

另核对 [官方 V4-Pro 固定包](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/tree/b5968e9190ef611bbf34a7229255be88a0e937c1)，
修订 `b5968e9190ef611bbf34a7229255be88a0e937c1`；`LICENSE`、`tokenizer.json`、
`tokenizer_config.json` 和 `encoding/encoding_dsv4.py` 四项 SHA-256 分别与上表一致。
这证明两个公开包的这些资产一致，不能据此证明托管 Pro-0813 的服务端完整请求转换也一致。

**缺口**：没有证据把该公开 V4 修订与当前托管 V4.1-Flash-1017 / Pro-0813 的 Tokenizer、
thinking 默认值、JSON 模式及服务端补充框架逐项等同；公开工具编码支持也不能证明完整 HTTP
请求到服务端输入 Tokens 的等价性，更不能证明所有输入的上界。因此不把它用于真实 strict 请求，
不回退到 V3/R1/其他模型 Tokenizer。DeepSeek 已承诺的 adapter v1 能力身份与 4096 输出限制保留，
没有把公开模型最大值偷偷写入旧身份。后续需独立版本化能力及证据后才可启用。
本地 Profile 也不能检测供应商在同一别名下静默替换服务端模型；启用真实严格保证前必须取得
可固定的服务端修订及其一致性承诺，不能仅把别名写进 Profile 就宣称已经绑定远端实现。

## 类型与信任边界

`TokenCounterProfile` 冻结绑定 provider、具体 model、adapter/capability 版本、计数方法和保证等级、
Tokenizer/template/framing 的版本或 Digest、窗口、模型输出上限、Profile 版本及 `evidence_digest`。
后者指向部署审核的证明/合规资料，不存 URL 凭据、Prompt、请求正文或原始结果。
Profile 由可信 Provider 部署依赖提供；Workflow/Task/模型/工具不能配置或升级它。

Profile 类型合法、Digest 匹配、有限测试向量通过都**不是自动认证数学证明**。部署方仍须独立审核
真实模型与框架等价证据，或 upper_bound 对全部受支持请求的证明；不能由被测 Counter 自己生成期望值。
本仓库没有登记任何真实供应商的 exact 或 upper_bound Profile。
计数器代码升级也须更新 method/Profile/证明身份，并核对部署包完整性；Core 不能检测维护者未声明的实现替换。

| 结果 | 允许的依据及行为 |
| --- | --- |
| `unknown` | 没有可信完整计数；`input_tokens=None`，不是 0。strict 在 Attempt/Permit 前拒绝；非严格保留供应商可能拒绝请求的兼容行为 |
| `exact` | 需要已审核的模型、模板、完整 framing 等价证明和固定资产。仅有文本 Token 数或一批 API Usage 比对不能宣称对未来完整请求 exact |
| `upper_bound` | 需要覆盖全部支持协议的保守上界证明。通常偏大的估算不成立；向量检查必须不低于独立已知真实输入数 |

旧的仅 `count()` Counter 不能再为新请求提供已知计数；需提供匹配的不可变 Profile。
方法、保证等级、窗口、模型输出上限或任一 Provider/模型/版本不匹配会在调用 Counter 之前拒绝。
Counter 可对不支持的有效载荷返回 unknown；strict 同样拒绝。计数异常只暴露 `ModelTokenCounterInvalid`。

## 计量与原子预约

顺序保持：完整历史校验 → Checkpoint/Egress/投影 → UTF-8 字节限制 → 冻结 Provider 请求及 Token
预检 → AgentBudget → Observed Usage Governance → Provider Admission → Permit/Attempt/Contract
原子预约 → 重新核对持久身份及执行租约 → 执行已冻结请求。

Counter 接收一次规范化 UTF-8 JSON 对应的完整 wire payload：包括 system/user/assistant/tool、
Core tool_call_id、多工具批次、工具描述及 Schema、Unicode/特殊 Token 文本、JSON response_format、
model、stream、输出及适配器参数。实现须按其已审核模板解释这些字段；不能把整个 JSON 的字节长度当作真实 Token 数。
只计最终选定投影一次，不在每个候选窗口上重复计数。Counter 不接受未投影的原上下文。
计数与执行都从同一份冻结 JSON 读取；原始消息字典在冻结后被并发修改也不能更换已计量的输入。

`TokenCount.input_tokens` 与 `ModelRequestContract.reserved_output_tokens`（即 max_output_tokens）分开。
在已有可信计数及窗口时始终检查：

```text
input_tokens + reserved_output_tokens <= context_window_tokens
```

等于允许，超出一个 Token 就拒绝。失败不增加模型预算、不创建 Attempt/Permit、不写 Usage，
只按现有 AgentStore 终态接口保存安全错误码；UNKNOWN、权限及历史记录不变。
Summary Generator 与 Consumer 复用同一 DurableRunner 检查，额外模型请求各自计数和预约，
Artifact 字节数、证据覆盖率和声明数量不能代替 Token 数。

## 持久身份与历史兼容

有 Profile 的新请求使用**RequestContract v2**，完整 Profile 参与 SHA-256、Session 配置身份及重试比较。
无 Profile 的兼容请求继续 v1，完全省略新键；旧 v1 摘要、Fixture 及读取验证保留，不回填 Profile 或 Tokens。
未知契约版本拒绝。v1 不能携带 Profile，v2 不能缺少 Profile。
同一逻辑调用的 Profile、资产、模板或配置变动时 fail-closed，不生成下一 Attempt；即使新计数相同也拒绝。
旧会话不能静默从 v1 改成 v2。先完成/安全终止旧会话，升级 Writer 后为新 Run 显式启用验证过的配置。

使用 0008 已有 Attempt.request_contract JSON，无 0009，无历史迁移改动。Store 恢复并重新验证
Contract/Profile/Projection；不在数据库事务内计数或读取外部资产。已有 8000 字节 Contract 限制与
会话容量预留适用；回归覆盖接近上限时 UNKNOWN/取消仍能落库。COMMIT 确认丢失后读取持久事实，
不因计数成功重发；模型 UNKNOWN、旧 token 和 Revision CAS 仍按原规则处理。

## 离线合规与执行成本

`infrastructure.models.token_conformance.verify_counter_conformance` 是可复用的启动前检查入口：
由操作者提供已固定的 Tokenizer、template、framing 和证据 bytes，核对 Profile SHA-256，
随后对最多 128 个公开固定 `TokenCounterVector(request_json, input_tokens)` 核对结果。
每个向量不超过 100 KB，每项资产不超过 32 MB；资产不执行、不自动下载。
exact 要相等，upper_bound 不低于独立期望值；损坏、漂移、异常统一安全拒绝。

部署方须在每个 Worker 启动时加载一次、验证并复用只读资产，审核其算法 CPU/内存边界。
实际请求计数输入限制 8.1 MB（8 MB 会话上限加配置开销），在 Worker 线程执行准备步骤，
避免阻塞事件循环；计数完成后仍须 CAS/有效租约才能预约。取消等待不会预约迟到的计数结果。
Core 无法强制终止有缺陷的同步 Counter 线程；不要接入不受信任、无限循环或联网的 Counter。
本轮未加载真实 Tokenizer，因此没有真实 Tokenizer 的加载/吞吐实测保证。

UTF-8 请求字节上限、文本 Tokenizer 数、完整 Chat 输入数、输出预约、供应商事后 Usage、
Observed Usage 软治理和真实费用硬预算是不同概念。预检不会创建 Usage 账目，
供应商 Usage 不自动证明账单或未来精确计数；真实费用硬预算与 exactly-once 结算仍未实现。

## 可重复验证入口

| 回归 | 主要证据 |
| --- | --- |
| `tests/unit/test_token_window.py` | Profile/资产/版本、完整公开 wire 向量、upper_bound oracle、窗口边界、过大计数输入与安全错误；只证明合成协议 |
| `tests/integration/test_durable_token_window.py` | 真实 Worker + Store，Projection/Egress/Checkpoint v3、生成与消费独立计数、延迟重试、Admission/Usage、UNKNOWN、取消计数线程 |
| `tests/integration/test_durable_model_request.py` | v1/v2 新预约边界、回滚、旧 token、独立 PostgreSQL 并发、控制容量预留 |
| `tests/integration/test_runtime_commit_faults.py` | v1/v2 提交回滚及确认丢失后依据持久事实恢复，不重复派发 |
| `tests/chaos/test_durable_runner_crash.py` | v2 预约/延期后的真实进程退出与新 Worker 接管，UNKNOWN 不重发 |
| `tests/integration/test_model_request_migrations.py` | SQLite/PostgreSQL 0008 历史升降级保留旧事实，不猜测回填计数身份 |
| `scripts/verify_durable_soak.py`、`scripts/verify_runtime_backup.py` | 保留旧场景，并增加纯合成 v2 Profile Run；真实独立进程及实际 pg_dump/pg_restore 后检查冻结身份 |

最终运行结果见 [RC 验收](durable-runtime-v1-rc-checklist.md)。上述测试不替代真实供应商合规证明。

**LIVE TOKEN CONFORMANCE NOT VERIFIED**。真实付费比对须另行显式授权、专用 Key、固定公开向量、
固定模型、请求/输出额度及超时；默认 pytest、CI 与本次验证都不发起付费请求。
