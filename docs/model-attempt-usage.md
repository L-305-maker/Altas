# Durable Model Attempt Ledger

该账本记录数据库已确认的模型尝试事实。供应商报告的 usage 不是 Core 独立核实的实际账单；
不知道用量时保留未知，不能作为零费用或零 token 的证据。

## 数据契约与事务

`ModelAttemptRecord` 位于 `application/ports/agent_store.py`，不依赖 SQLAlchemy。新增表
`agent_model_attempts` 使用 `(session_id, model_call_id, attempt_index)` 复合主键和逻辑调用复合外键。
`attempt_index` 从每个逻辑调用的 1 开始连续递增。记录还包含 outcome、accounting、可空 model /
request_id、raw_usage、规范化 usage、created_at 和 confirmed_at（数据库时钟）。
model 与 request_id 各限 256 字符。后者仅是供应商元数据，不是幂等键或独立费用确认依据。

| 操作 / 结果 | 持久化行为 |
| --- | --- |
| READY_MODEL → MODEL_INFLIGHT | 与 Snapshot / ModelCall 一起新增 INFLIGHT 尝试；增加一次 model_attempts |
| 明确成功 | 与 assistant / Action 或成功 Step / Artifact 一起确认 SUCCEEDED，保存合法 usage 和元数据 |
| 明确可重试失败（例如 429） | 确认 FAILED，与 READY_MODEL、未来 available_at 及租约释放一起提交；下次使用同一调用 ID、新索引 |
| 明确不可恢复失败 / 截断最终回答 | 确认 FAILED；合法响应中已报告的 usage 仍保留 |
| 取消、结果未知、预约后崩溃 / 租约恢复 | 确认 UNKNOWN，无 usage；恢复不重发，不改成可重试 FAILED |
| 数据库回滚 | Snapshot、调用与尝试一起回滚；预约失败不留孤立记录，结果失败保留之前的 INFLIGHT |
| COMMIT 成功但确认丢失 | 新 Worker 重读账本，不重复记账或重发；旧 revision / token 的重复提交被拒绝 |

UNKNOWN 的 confirmed_at 表示“不确定性已持久记录”的时刻，不是供应商完成时间。
UNKNOWN 可随 Agent 的 fail-closed 终止再次传递，但不改写尝试的原记录或确认时间；本阶段没有
模型结果人工补账 API。SUCCEEDED / FAILED 尝试不能再确认或派发。

锁顺序保持 Run → Step → AgentSession → ModelCall → Attempt → Action，审批锁沿用既有
Session 后的顺序。所有执行写入检查数据库时钟租约、原 token、Revision CAS 和 `advance()`。
Workflow 取消后旧 Worker 无法追加用量。查询汇总不改变任何持久状态。

## 用量与汇总

`ModelUsage` 的 input_tokens / output_tokens / total_tokens 是可空的严格非负整数，拒绝 bool、
负数、浮点数、字符串、NaN 和 Infinity。`source` 只有 provider_reported / unavailable。
`normalize_usage` 仅映射明确存在且合法的值；不相加补全 total，不从 total 推导缺失字段。
DeepSeek 映射 prompt_tokens → input_tokens、completion_tokens → output_tokens，其他合法字段
原样保留在 raw_usage；通用 Provider 可以提供自己的映射及规范化值。原 `complete()` 接口保持兼容。
适配器属于可信应用边界，Core 不独立验证供应商计量算法。

无效的单个计数保留在有限 JSON 原始记录中，但其通用规范值为 None，其他有效字段仍可计量。
NaN / Infinity、非 JSON、过大 usage 或无效响应结构会被安全拒绝，按协议失败持久化未知用量，
不保存不安全正文、不伪造计数。成功响应没有 usage 时，仍是 SUCCEEDED + unavailable。

`AgentSession.model_attempts` 按模型轮次和 attempt_index 恢复。`usage_summary()` 对三个字段分别返回：

- value：已报告值的部分和；没有任何测量时为 None，已报告的 0 则为 0。
- reported_attempts / unreported_attempts：该字段的覆盖数，之和等于所有尝试数。

汇总另含 inflight_attempts、unknown_outcomes、legacy_unmeasured_attempts 和 unavailable_usage_attempts。
一次尝试可能 outcome 已确认但 usage 未知，也可能只报告部分字段；三个总数不能相互推导。
示例：429 无用量，重试成功报告 input=7/output=3、无 total。input.value=7、覆盖 1/2，
output.value=3、覆盖 1/2，total.value=None、覆盖 0/2。这不是一次完整的总消耗声明。
`SQLAgentStore.usage_summary(session_id)` 可在终态读取；调用方负责授权，不新增运维 HTTP 接口。

raw_usage 和规范化 usage 各限 16 KB，仍拒绝非有限 JSON；正常新尝试计入原 8 MB 会话配额。
预约为逻辑调用 raw_usage、尝试 raw_usage、规范化整数及元数据预留容量，容量不足在请求前拒绝。
不会为了保存用量而删除消息、行动或审计，不静默截断。原始用量、request_id、Prompt 和响应正文
不进入 EventRow、普通日志或 Prometheus labels。数据库中的这些字段仍需部署方保护和保留策略。

## 升级及回退

迁移 `0004_agent_model_attempt_usage` 的 down_revision 为 0003，0001–0003 不修改。
停止所有 Writer / Worker 并备份后升级。新安装可直接 upgrade head；SQLite 和 PostgreSQL 使用相同契约。
`create_schema()` 只适合新库学习模式，不能为已有数据完成 backfill。

对每个旧逻辑调用保留 attempts 数值并创建对应索引的 LEGACY_UNMEASURED 记录；最后一项可以
保留逻辑调用当时已知的状态，之前各次 outcome 本身也标记 LEGACY_UNMEASURED。
旧记录无法证明逐次预约/确认时刻，因此时间留空；旧调用 usage 留在原表，不归属到某次尝试。
这些迁移生成的元数据不追溯占用旧会话配额，避免使接近原限额的会话不可读；新预约全部计入配额。
旧的 FAILED 调用恢复后可新增已计量来源的预约，旧的 INFLIGHT 则遵循 UNKNOWN 恢复规则。

显式 downgrade 0003 删除新账本，保留其他表及旧聚合；这会永久丢失新增的逐尝试事实。
再次升级只能生成 LEGACY_UNMEASURED，不能恢复已丢掉的计量证据。回退前必须备份，不是无损账本回退。
备份恢复脚本比较全部 13 张表，包含已知 usage、审批、对账及未来重试时间。

## 治理边界

当前只实现 measured usage / soft governance，模型轮次、尝试数及工具次数仍是硬预算。
Durable 配置继续要求 max_resource_units=0。usage 不转换成 resource_units 或货币；没有版本化价格、
供应商账单核对或请求前资源上界预约。UNKNOWN 可能产生费用，未报告 usage 也可能产生费用。
后续 Provider Admission Policy 和硬 Resource Budget 应独立设计、先验证请求前预约策略，
确保供应商返回结果不会因事后超额而无法提交。这里不承诺真实费用 exactly-once。

## 回归证据

- `tests/unit/test_model_usage.py` 和 `test_model_response.py`：严格整数、缺失/零、供应商字段映射、
  合法扩展字段、有限 JSON 和大小限制；全部使用离线数据或 MockTransport。
- `tests/integration/test_model_attempt_usage.py`：真实 Worker 延期恢复、逐次事实、部分汇总、
  写入回滚、重复结果、旧 token、取消、UNKNOWN 不可改写和独立 PostgreSQL 结果竞争。
- `test_model_attempt_migrations.py`：SQLite / PostgreSQL 升级、显式回退、旧数据与 Artifact 保留、
  LEGACY_UNMEASURED 和旧 FAILED / INFLIGHT 的安全恢复。
- 现有 `test_runtime_commit_faults.py` 与子进程 `test_durable_runner_crash.py` 同时核查账本，
  覆盖预约前后退出、结果 COMMIT 回滚/确认丢失、延期退出和新 Worker 恢复。
- `tests/runtime_assertions.py` 核查尝试总数与预算相等、历史确认记录不被覆盖，复用于审批、
  对账、取消、多 Worker DAG 和 Soak；备份恢复逐表比对包含账本的全部 13 张表。

旧 AgentStore 回归曾允许将租约恢复后的 UNKNOWN 手动改写为 FAILED 后重试，本次按任务约束收紧：
该路径现在拒绝写入并测试 fail-closed 终止，保留同一 UNKNOWN 尝试和已消耗预算。普通确认失败的
延期重试路径不变。

### 本地验证（2026-10-17）

| 命令 / 演练 | 实际结果 |
| --- | --- |
| `uv run ruff check src tests scripts migrations evals` | PASS |
| `uv run ruff format --check src tests scripts migrations evals` | PASS，122 文件 |
| `uv run pytest -q` | 648 passed、230 skipped；1 条既有 Starlette 弃用警告；跳过不计通过 |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 445 passed、零跳过；真实临时 PostgreSQL、独立事务、迁移、取消/审批/对账/故障恢复 |
| 临时 SQLite Alembic CLI：upgrade head → check → downgrade 0003 → upgrade head → check | PASS，无模型差异 |
| `uv build` | PASS，sdist / wheel |
| `uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` | PASS；仓库外安装、用量汇总、参考应用、Durable Runtime、认证指标 |
| `uv run python evals/run.py` | PASS，3/3 离线用例 |
| `uv run python scripts/verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；0004、13 表、7 Run，已完成工具重派 0 |
| `uv run python scripts/verify_durable_soak.py --bin D:/PSQL/bin --seconds 30 --workload 6 --seed 7` | PASS；36.38 秒、23 Run、41 次 claim；独立三 Worker 进程 |

本机 Docker daemon 不可用，Docker / Compose 本地 NOT VERIFIED，交由既有 GitHub Actions 实际执行。
LIVE PROVIDER NOT VERIFIED：未添加付费测试选项，也未调用真实 API；MockTransport 不算真实模型联调。
真实跨主机网络故障仍 NOT VERIFIED，单机多进程结果不代替该项。长时间稳定性历史证据仍见 RC checklist，
本次只运行上述有界样本。生产发布仍须遵循既有 RC 和备份恢复检查表。
