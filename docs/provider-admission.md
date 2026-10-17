# Provider Admission Control v1

准入在现有 PostgreSQL / AgentStore 事务中协调多个 Worker 和 AgentSession；没有新队列、调度器、
HTTP 运维接口或外部配额服务。它限制受本协议管理的请求预约，不是供应商计费系统。

## 五种独立约束

| 概念 | 范围与语义 |
| --- | --- |
| Request Admission | 同一可信 provider_key 的未释放且未过期许可容量，以及滑动窗口内的全部预约数量 |
| Model Attempt Budget | 每个 Session 的模型轮次、尝试和工具次数硬限制；只有真正持久预约才增加 |
| Provider Usage Ledger | 每次尝试的已知结果、provider-reported usage 或 unavailable / UNKNOWN，不将缺失当零 |
| Soft Resource Governance | 可查询的已报告用量之和及未报告覆盖数，不独立确认账单 |
| Hard Cost Budget | 尚未实现；max_resource_units 仍不启用，没有请求前费用上界或 exactly-once 结算 |

## 可信配置与启用

`ProviderAdmissionPolicy` 是冻结且 extra=forbid 的类型。仅由部署 Settings 或可信 Python 组合入口注入：

```sh
AGENTFLOW_PROVIDER_ADMISSION='{"provider_key":"shared-model-prod","max_concurrent_requests":4,"max_requests_per_window":60,"window_seconds":60,"admission_retry_delay_seconds":1,"permit_ttl_seconds":120,"policy_version":"1"}'
```

该值不是 API Key。provider_key 只使用非敏感稳定标签，须表示所有相关进程共享的供应商配额范围，
不能用密钥、Prompt、用户/Run/Action ID 或供应商 request_id。相同凭据、模型或供应商账户是否共享
额度由部署者确定，不由模型猜测。计数限 1–1,000,000，窗口及 TTL 限 1–86,400 秒，
重新准入间隔限 0.1–3,600 秒，浮点必须有限，次数必须严格整数；不允许零间隔忙循环。

bootstrap 将策略注入使用 Worker 原 Store/Engine 的 SQLAgentStore，并将其作为
`ProviderAdmissionStore` 传给 DurableAgentRunner。自定义组合入口同样显式传 `admission=agents`；
普通 `commit_transition` 不能替代已绑定会话的准入预约。Prompt、Task.inputs、dependencies、工具参数
均不作为策略来源；DurableAgentConfig 也不接收这些额度字段。默认 None 保持现有调用路径。
旧 AgentRunner 与普通 Handler 不受本功能治理，行为保持兼容。

数据库保存首次使用的完整规范策略，包含 policy_version；同 key 的字段或版本不一致会 StateConflict
安全停止，不各自发放额度。v1 不提供在线策略编辑接口。配置变更应停所有相关 Writer/Worker，排空
已知请求，并为旧 UNKNOWN、TTL 和窗口留出保守时间，再制定受控切换；不能用随意换 key 绕过额度。
一个 Session 首次启用后固定绑定该 key，未配置或不同 key 的 Worker 不能为它预约新请求。
旧未绑定 Session 可在下一次 READY_MODEL 预约时绑定，不追溯为旧尝试伪造许可。

所有相关 Durable Worker 必须一致启用。数据库无法识别完全绕过此协议的旧程序、手写 HTTP 或其他
供应商账户流量；升级和启用前应停旧 Worker、等待旧请求/窗口或按供应商事实人工核对。不宣称能为
未被记录的旧请求补建准确配额历史。生产库必须使用 Alembic，create_schema 不能代替升级。

## 数据与锁顺序

0005（down_revision=0004）新增：

- `provider_admissions`：provider_key 主键、不可混用的策略 JSON、cooldown_until、内部 revision 与创建时间。
- `provider_permits`：许可 UUID、provider_key 外键、尝试复合外键、原 token 的 SHA-256、预约 revision、
  active/released/unknown、创建/到期/释放时间。`(session_id, model_call_id, attempt_index)` 唯一。
- `agent_sessions.provider_key` 可空绑定，旧行默认保持未绑定。0001–0004 不修改。

许可保留完整历史，ModelPermit 只读契约不暴露 token 或摘要；它不是授权凭证。数据库本身仍需保护。
窗口和活跃查询分别有 `(provider_key, created_at)`、`(provider_key, status, expires_at)` 索引。
速率直接来自窗口内持久许可，不依赖进程 Counter；内部配额 revision 仅用于事务序列化，不是请求数或费用。
许可属于会话的有限元数据，计入既有 8 MB 配额和控制余量，不删除原审计或静默截断。

先锁 Run → Step → AgentSession → 已有 ModelCall → Attempt → Action（沿用审批锁顺序），
最后锁单个 Provider 配额行。新尝试/许可在已锁 Session 内插入。持 Provider 锁后不再锁其他
Session / Run / Attempt，也不进行其他会话的过期清扫。结果事务同样先锁所属执行记录再锁 Provider。
许可到期通过数据库时钟谓词判断，不通过持全局锁后更新别人的记录来回收。

默认 PostgreSQL READ COMMITTED；策略行在发放及结果处理时递增内部 revision，使较强隔离下的
陈旧快照发生序列化冲突并安全停止。真实 REPEATABLE READ 回归先复现过“双发”，修复后只有一个
预约成功，另一个事务停止；没有隐藏重试或网络请求。连接、锁/语句超时沿用部署设置，序列化/数据库
异常映射 AgentStoreUnavailable。SQLite 使用既有 BEGIN IMMEDIATE 串行写，支持功能与迁移学习测试，
不能替代 PostgreSQL 行锁、跨主机或生产高可用验证。

## 预约和延期事务

`ProviderAdmissionStore.reserve_model_attempt(task, transition)` 返回类型化 ModelAdmission：

1. 验证未终止 Run、原 token、数据库时钟租约；加锁恢复 Session 和历史。
2. 验证 READY_MODEL → MODEL_INFLIGHT 候选、revision+1、advance() 和固定预算。
3. 锁 Provider 行，核对完整策略，锁后重读 DB 时钟；计算窗口全部预约和未释放/未到期活跃数。
4. 可用时，许可、ModelAttempt INFLIGHT、ModelCall 和 Snapshot 原子提交。提交确认后才返回 reserved。
5. 不可用时，不新增 ModelCall、Attempt 或许可；READY_MODEL 同状态 revision+1，预算、完成的 Action、
   历史及逻辑调用 ID 不变；Step ready、未来 available_at、continuation_pending=true，清空租约/token。
6. 返回 admission_deferred，Worker 从数据库验证状态与错误码，不再 complete/fail。正常 claim 续执行
   不增加虚假的 Step 失败 attempts；真正租约失败继续受原 max_attempts / retry_delay 限制。

并发满额时按固定最小重试间隔重试准入，可在已知完成释放后推进；速率满额还等待最早预约离开窗口，
cooldown 则至少等待其截止时刻。所有时间持久化，重启不会重新计算成更早的等待时间。Worker 不持租约
sleep，不在 SQL 事务中调用 Provider。下一轮仍先检查上下文大小、硬调用预算，然后才申请许可。

`validate_model_dispatch(task, permit)` 在 HTTP 前再次验证原 token、当前 Snapshot/Attempt 和未过期许可。
重复预约受到 CAS 和唯一键拒绝；新 token 不能使用旧许可确认成功/失败或再次派发，但可以保守记录 UNKNOWN。
提交确认丢失时不调用 Provider，恢复只读取已提交事实：MODEL_INFLIGHT 不重发，已提交的延期按原 deadline 领取。
数据库回滚不留下孤立许可、预算或已调度假象。

## 结果、429、UNKNOWN 与 TTL

| 事实 | 活跃容量 | 滑动速率窗口 |
| --- | --- | --- |
| 已确认成功 | 同事务 released，保存结果和可用 usage | 仍计入原预约 |
| 已确认可重试失败 / 配置或协议失败 | 同事务 released；只有确认可重试失败允许模型延期重试 | 不退款 |
| 明确 HTTP 429 | FAILED + released，原子提升同 key cooldown；其他 key 独立 | 不退款 |
| 模型 UNKNOWN / 取消 | unknown，保留原 expires_at、released_at 为空 | 不退款 |
| 进程退出 / 丢租约 / 提交结果不确定 | 先保留 active；租约恢复记录 UNKNOWN，固定 TTL 不延长 | 不退款 |
| TTL 到期 | active/unknown 在容量查询中不再计入；不改写原事实或重新派发 | 直到原窗口自然退出仍计入 |

DeepSeek 429 使用向后兼容的 ModelRetryableError 子类 ModelRateLimitedError；普通连接失败不会仅因
有 retry hint 就产生全局冷却。非法/负数/非有限 Retry-After 忽略；全局提示最多 3,600 秒，且至少为
可信策略的 admission_retry_delay_seconds。耗尽 Agent 尝试预算时也会保存 FAILED 和冷却事实，
其他会话看得到。模型内部退避还受原策略控制；Step deadline 不早于全局 cooldown。
冷却阻止后续准入，不追溯撤销已发出的有效许可。Provider 适配器不做隐藏重试。

Tool UNKNOWN 与模型 UNKNOWN 必须区分：审批或工具对账等待期间，已确认模型响应的许可已经释放，
不会因为工具等待占用模型容量。人工对账仍使用原动作、审批、幂等键和消息历史。

TTL 不是外部执行已停止的证据。一个旧 HTTP 请求可能在 TTL 后仍执行，此时新请求可以获得逻辑容量，
物理并发会超过设定值；请求也可能产生未知费用。建议 TTL 大于供应商超时及清理余量，但不能因此保证
网络 exactly-once。TTL 不续期。若旧请求稍后明确返回且原 Task 租约仍有效、Attempt 仍 INFLIGHT，
可保存真实结果并 released，不能因 TTL 过期丢掉已知用量；若已经 UNKNOWN 或丢租约则拒绝确认。
HTTP 前重查与实际发送之间仍有取消/断网窗口，依赖原 fencing、UNKNOWN 和租约恢复，不能原子提交外部请求。
原 CancelledError 向上传播，最多五秒清理等待不变；不能强制终止数据库驱动同步线程。

## 升级、备份与验证边界

SQLite / PostgreSQL 的 0004 → 0005 → 0004 回归保留 Workflow、Snapshot、Action、审批/对账、
ModelAttempt usage 和 Artifact。显式回退会删除新配额/许可/绑定，丢失窗口事实；必须先停 Writer、
备份、排空请求并保守等待旧窗口/TTL，不能用 downgrade/upgrade 重置配额。再升级不伪造历史许可。

备份恢复脚本使用隔离 PostgreSQL 的真实 pg_dump / pg_restore 比较全部 15 表，固定策略由测试控制器
注入三个独立 Worker；包含 released、模型 UNKNOWN 保留许可、额度延期、模型重试、审批/对账和取消。
恢复库保留原许可 ID、token 摘要、截止时间、策略修订号和账本，不重放 UNKNOWN 或已完成 Action。
这不是供应商账单对账、PITR/主从切换、跨主机分区或零数据丢失证明。

新增回归在 `test_provider_admission_policy.py`、`test_provider_admission.py`、
`test_provider_admission_migrations.py`、`test_provider_admission_crash.py`；复用真实 claim、
共享运行时不变量、独立 PostgreSQL 事务和受控数据库时间推进。核心用例进入既有 PostgreSQL 专项，
不增加重复大型矩阵。发行 wheel 的离线 smoke 也显式启用准入。真实付费 Provider 默认不运行。

## 本次本地验证记录

2026-10-17，Windows / Python 3.12，真实 PostgreSQL binaries：`D:/PSQL/bin`。

| 命令 / 验收 | 实际结果 |
| --- | --- |
| `uv run pytest -q`，最终再用 `-rs` 核对跳过原因 | 678 passed、255 skipped；跳过不算通过 |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 491 passed，包含独立连接并发、fencing、取消、审批/对账与迁移 |
| 最终准入、0005 迁移及进程崩溃测试，独立临时 PostgreSQL | 46 passed；以共享数据库时钟偏移推进到期，不改写许可历史 |
| `uv run ruff check src tests scripts migrations evals` | 通过 |
| `uv run ruff format --check src tests scripts migrations evals` | 129 files already formatted |
| `uv build`、`uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` | 构建、安装后参考应用、Durable 准入与 metrics smoke 通过 |
| `uv run python evals/run.py` | offline 3/3 |
| `uv run python scripts/verify_durable_soak.py --bin D:/PSQL/bin --seconds 30 --workload 6 --seed 7` | PASS；38.56 秒、29 Runs、3 个独立 Worker 进程；默认关闭准入的兼容路径 |
| `uv run python scripts/verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；0005、15 表、10 Runs，启用准入的三个独立 Worker；保留 UNKNOWN 许可、429 冷却和延期时间，已执行工具重放 0 |
| 架构边界专项 | 3 passed；完整测试也包含该检查 |

全量跳过包括未配置专用 URL 的 PostgreSQL 参数化测试（已由独立临时实例专项实际执行）、
两个 Docker opt-in 用例和一个显式付费开关关闭的 live 用例。已有 Starlette/httpx 弃用警告保留。
本地 Docker 引擎不可用，Sandbox / Compose 本地为 NOT VERIFIED，交由既有 GitHub CI 实际运行；
不能用普通测试的跳过结果代替。真实付费 Provider 和跨主机网络分区演练均 NOT VERIFIED。
本文件记录本地证据；准确提交的 GitHub Actions 状态另见交付报告与对应 main 运行记录。
