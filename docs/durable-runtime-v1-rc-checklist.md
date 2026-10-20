# Durable Runtime v1：Release Candidate 验收

## 结论与基线

PR #9 的原合并提交 `40862a93bc7db2a53bdcd4f320c367ed468db33a` 位于旧开发分支，并未将
RC 能力集成到 main。main 撤回提交 `3a25db0e295c21453892c1adff467436035b6055` 的代码树与
基准 `8fbb6e12b1ab3093ba9ae5f211da94b21aaac97e` 相同。本次仅在 main 应用这两个代码树之间
已审核的差异，以 `f26f192` 保存集成提交；其代码树与原 PR #9 合并树完全相同，再补三项安全修复。
没有重写历史、创建分支或 PR，也没有修改旧合并记录。本页原 PR9 证据为历史记录，本轮结果另列于下方；
后续部署必须重做环境项。

**代码可审核/合并不等于当前环境获准生产发布。** 建议进入隔离 RC staging 验收；真实 Provider、
跨主机和生产配置仍未验证，不能宣称 Production Ready。外部副作用不保证 exactly-once。

状态定义：PASS 实际执行通过；FAIL 实际执行失败；NOT VERIFIED 未具备环境/授权或尚未执行；
NOT APPLICABLE 只用于明确不适用的部署项并注明理由，不用来代替缺失证据。

## 验收矩阵

| 项目 | 状态 | 证据 / 发布条件 |
| --- | --- | --- |
| Domain、预算、顺序、终态 | PASS | 完整 pytest：606 passed、204 skipped；专用门控单列 |
| AgentStore CAS / fencing / Worker 取消 | PASS | 真实临时 PostgreSQL release-gates（结果见下） |
| Runner、审批、UNKNOWN 对账、重试、上下文 | PASS | 完整回归、真实 PG 及 soak 组合验证 |
| PostgreSQL 备份恢复 | PASS | 实际 pg_dump/pg_restore，12 表、7 Run 全量事实一致；已执行工具重派 0，待审批新批准派发 1 |
| 单机独立 Worker 稳定性 | PASS | 180 秒目标实际 194.97 秒，29 波、179 Run、91 Action、298 次领取；最终短样本 34.24 秒 |
| 跨主机部署 / 网络分区 | NOT VERIFIED | 无双宿主；见 recovery runbook 的 staging 入口 |
| 真实 DeepSeek API | NOT VERIFIED | 未提供专用付费授权/Key，本轮真实请求 0；默认 live 跳过 |
| 指标故障与只读核查 | PASS | COMMIT 后真进程退出、三进程共享目录/API、坏目录、本地重启与只读审计 |
| 数据库迁移和历史审计 | PASS | 0001–0003 不修改，临时 PG 迁移回归和恢复库 upgrade head |
| 发行包 / 安装 wheel | PASS | sdist/wheel 构建，仓库外安装、Durable/认证指标/参考应用成功 |
| Docker Sandbox / Compose | NOT VERIFIED | 本地 daemon 状态与本分支 CI 另行记录，不借用 Mock 结果 |
| 架构 / 参考应用 / 离线评估 | PASS | 架构 3 passed；参考应用回归及 offline 3/3 |
| 生产数据库连接 / 超时 / 租约 / 心跳 | NOT VERIFIED | 下表是验收条件，不是当前生产实测 |
| HA 切换 / PITR / 零 RPO | NOT APPLICABLE | 本轮只实现逻辑备份恢复门禁；需要这些 SLA 的部署须另行演练 |

## 实际执行记录

| 命令 | 本地实际结果 |
| --- | --- |
| `uv run pytest -q` | 606 passed、204 skipped，126.05 秒；1 项已有 Starlette TestClient 弃用警告 |
| `uv run ruff check src tests scripts migrations evals` | PASS |
| `uv run ruff format --check src tests scripts migrations evals` | 116 files already formatted |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 394 passed，零跳过，241.73 秒；真实临时 PG，含 SQLite 对照 |
| `uv run python scripts/verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7` | PASS；194.97 秒、29 波、179 Run、91 不同 Action 派发、298 次领取 |
| 同脚本 `--seconds 30 --workload 6 --seed 7` | PASS；34.24 秒、3 波、23 Run、13 Action、41 次领取；最终记录/权限/deadline 检查 |
| `uv run python scripts/verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；12 表、0003、7 Run；已完成 Action 重派 0，原待审批 Action 明确批准后新派发 1 |
| `uv build` | wheel + sdist 成功 |
| `uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` | 仓库外安装、参考应用、Durable Step/Artifact、认证指标及离线评估成功 |
| `uv run pytest -q tests/unit/test_architecture_boundaries.py` | 3 passed |
| `uv run python evals/run.py` | offline 3/3 |
| `uv run pytest -q tests/integration/test_docker_sandbox.py` | 2 skipped，本地无 daemon，未计通过 |
| `docker info` / `docker compose config --quiet` | daemon pipe 不存在；Compose 缺部署环境变量，本地 E2E 未执行 |
| `uv run python scripts/verify_live_provider.py` | LIVE PROVIDER NOT VERIFIED，requests=0 |

最后增加损坏 Step 状态的脱敏失败回归，再改为只输出固定 invalid 分类。
同一审计文件在新建临时 PostgreSQL 上运行双后端 **6 passed、零跳过**，Ruff/格式检查再次通过。

204 skipped 包含专用 PG、Docker 和 1 项显式 live 门控，不计通过。真实 PostgreSQL 由独立临时实例
专项补充。本分支 GitHub CI 的实际结果在 PR 中记录；本地 Docker 不可用不借用上游 CI 的成功替代。
MockProvider/MockTransport 不是供应商实测。完整故障窗口复用 [v1 verification](durable-runtime-v1-verification.md)。

新增验证先执行失败回归：审计入口缺失、隔离资源入口缺失、live 双重授权及 Durable harness 缺失；
再实现入口。实际演练也发现测试断言遗漏“崩溃对账后真实有界 Step 重试”的 ready 状态，以及被取消
前尚未创建 Session 的 Task。仅修正测试检查器/验证器，不修改 Domain 或运行时状态规则。

## main 集成与安全修复验证

1. 干净基准完整回归：590 passed、196 skipped；恢复原 PR9 后完整回归：606 passed、204 skipped。
   先验证原始 30 秒 Soak、真实 7 Run/12 表备份恢复和 Ruff，再创建集成提交。
2. 安全回归先失败后实现：实际 Worker 日志含 PRIVATE Task 数据、环境变量独自启用 live、缺少分页。
   测试拦截付费脚本，失败也不能发送真实请求；失败输出只使用安全错误码。
3. JSONL 只留租约身份，保存在每 Worker 私有目录；原 token 保持不变，用真实 Store.claim 的 Task
   验证 load/commit_transition 均 LostLease。Action ID、幂等键和所有持久化不变量保持原检查。
4. 默认 pytest 即使有模拟付费授权与 Key 仍跳过；显式选项 + 授权 + Key 的启用路径只用 MockProvider
   回归。独立脚本仍需要 --allow-paid-request，手动 Provider Job 仍默认关闭。
5. 审计新增有界 offset/next_offset，每页最多 10000 Step、明确排序、只读、无原始身份输出。跨页是
   独立快照，更新时可能移动，不声称多页构成一次全库一致快照。

本轮本地实际结果见下表；main CI 在推送后独立核对，不把跳过项记为通过。

| 检查 | 本轮结果 |
| --- | --- |
| `uv run pytest -q tests/unit/test_rc_tools.py` | 15 passed |
| `uv run pytest -q tests/integration/test_runtime_audit.py` | 8 passed、8 skipped（PG 专项另测） |
| `uv run pytest -q tests/integration/test_runtime_metrics_loss.py` | 4 passed、4 skipped（PG 专项另测） |
| `uv run pytest -q tests/integration/test_rc_fencing.py` | 1 passed、1 skipped（PG 专项另测） |
| `uv run pytest -q`，模拟付费授权/Key 均存在、未给 live 选项 | 618 passed、210 skipped，真实付费请求 0 |
| Ruff check / format --check | PASS；117 files already formatted |
| `uv build` / `scripts/verify_wheel.py` | PASS；仓库外安装、Durable/认证指标/参考应用/离线评估 |
| 架构检查 / `evals/run.py` | 3 passed / offline 3/3 |
| `verify_postgres.py --bin D:/PSQL/bin --release-gates` | 406 passed、零跳过，164.36 秒；真实临时 PG，含 SQLite 对照 |
| `verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7` | PASS；190.47 秒、34 波、209 Run、106 Action 派发、346 次领取 |
| `verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；0003、12 表、7 Run；已完成工具重派 0，明确新批准派发 1 |
| 本地 Docker Sandbox | 2 skipped；daemon pipe 不可用，不计通过 |
| 最新 main Docker/Compose/全部 CI | 推送后核对实际运行；最终交付报告提供链接，不借用旧分支结果 |

真实 DeepSeek 与跨主机仍为 NOT VERIFIED；本轮无新增 Core、迁移、Scheduler 或授权语义改动。

## 运维关注项

| 检查 | 部署验收条件 |
| --- | --- |
| PostgreSQL 身份和网络 | 专用最小权限角色、TLS/网络边界、Secret 不入日志，测试库与生产库隔离 |
| 连接池 | 按 API + 每个 Worker 的池上限核算总连接数，使用有限 connect_timeout；预留备份/运维连接 |
| statement_timeout / lock_timeout | 在 Worker 角色/连接配置有界超时，验证实际生效；演练使用 5s / 2s，不直接宣称适合所有生产负载 |
| 租约 / 心跳 | 心跳周期小于 lease（当前 Worker 为 lease/3），为数据库超时、调度延迟留余量，旧 token fencing 生效 |
| 取消清理 | 最多 5 秒协程等待；不能强制结束同步数据库线程，迟到提交依赖原 token/revision 校验 |
| 工具审批 | 工具实现变更更新版本；授权绑定版本、策略、参数 digest；已消耗审批不得复用 |
| 人工等待 | WAITING_APPROVAL / RECONCILIATION 配值班 SLA；等待久是提醒，UNKNOWN 不能自动重派 |
| 延期重试 | 数据库时钟 available_at、持久 continuation、相同逻辑模型 ID；模型预算与 Step 失败 attempts 独立 |
| 上下文 | UTF-8 序列化字节上限不是 token/费用限额，不截断协议或删除审计历史 |
| 指标 | 显式启用、认证、有限 labels；本地重启/提交确认丢失可少计；共享目录必须受保护且停部署后清理 |
| 持久核查 | SELECT-only 角色，限定 since/limit/超时，truncated 报告不是全库健康证明；退出 1/2 触发调查 |
| 备份 | 加密、权限/保留期、停止 Writer 顺序、隔离新库恢复、核对备份之后的外部副作用 |
| RC 放行 | 本版本 CI 通过 + 目标环境演练记录 + 人工发布授权；长测单独手动门禁 |

Prometheus 与数据库不是逐事件相等的双账本。审计仅查询当前持久事实，不重建全部历史、不修改业务数据。
有限 seed/时长无法穷举所有交错；真实跨主机、生产负载及真实 Provider 证据应附在部署验收记录。

## Context Projection v1 验收（2026-10-18）

基线 main 为 15d75c2f6dee1c92e784278f30b1b31ac557efa4，其 GitHub CI 已核实成功。
本次只新增确定性投影及逐尝试 Manifest，不修改 Action/Agent Domain、Scheduler、审批或对账规则。
详细配置、算法与迁移契约见 [Context Projection](context-projection.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 完整 pytest | PASS | 742 passed、294 skipped，114.75 秒；1 项已有 TestClient 弃用警告 |
| Ruff check / format --check | PASS | src tests scripts migrations evals；141 files already formatted |
| 新旧消息视图 | PASS | 合成 3 轮、每轮 2 个 Action，含 Schema：1766 → 686 UTF-8 字节，保留 1 轮、省略 2 轮 |
| 真实 Worker 与历史不变 | PASS | full 在 1500 字节限额停止；recent_complete_rounds 完成，原消息、结果、Action 与 Artifact 断言不变 |
| 重试 / UNKNOWN / COMMIT 确认丢失 | PASS | 同逻辑调用复用 Manifest；Schema 漂移、旧无 Manifest 重试保守停止；UNKNOWN 不重发 |
| 准入 / 观察用量 / 取消容量 | PASS | 投影不绕过 Permit 与软阈值；Manifest 计入会话限额，近容量取消仍可保存 UNKNOWN |
| SQLite 与 Alembic | PASS | 0007 升级、回退到 0006、再升级、check；原数据不变，旧 Manifest 为 None |
| 真实 PostgreSQL 完整发布门禁 | PASS | verify_postgres.py --bin D:/PSQL/bin --release-gates：566 passed、零跳过，230.29 秒；含独立事务 CAS、真实旧 token、取消、审批、对账及迁移 |
| 三独立 Worker Soak | PASS | 30 秒目标，实际 34.22 秒，seed=7，23 Run、13 Action、41 领取；单机隔离临时 PG |
| 真实 pg_dump / pg_restore | PASS | 0007、15 表、10 Run 全量事实一致（含 Manifest）；已完成工具重派 0，明确新批准派发 1 |
| 构建 / 仓库外 Wheel 安装 | PASS | sdist + wheel；投影配置、Durable Step、许可、用量、认证指标及参考应用 |
| 架构 / 离线评估 | PASS | 架构边界 3 passed；参考应用 offline 3/3 |
| 本地 Docker Sandbox / Compose | NOT VERIFIED | daemon pipe 不可用；Sandbox 2 skipped；最终 main CI 结果在交付记录中单列 |
| 付费 Provider / 跨主机网络分区 | NOT VERIFIED | 真实模型请求 0；无跨主机测试环境，不用 Mock 或单机 Soak 替代 |
| 生产环境部署放行 | NOT VERIFIED | 仍需目标环境连接/超时/租约、工具版本、外部副作用与人工运维验收 |

294 skipped 不计通过，主要是独立 PostgreSQL、Docker 和显式付费门控；PG 由专用临时实例补充。
投影专用 PG 用例进入原 verify_postgres.py 门禁，未新增或关闭 CI Job。

专项曾出现 564 passed / 1 failed：原三 Worker 测试把并发下合法的空 tick 当作失败。
Store.claim 使用 SKIP LOCKED，资格扫描与实际 Step 加锁之间也可丢失本次领取机会，正常 Worker 循环会继续轮询。
只将测试改为原 15 秒总超时内轮询，并额外断言三个不同 Run 恰好各被领取一次；仍要求 1 个 Permit/Attempt、
2 个持久延期。新增真实 PG 行锁空领取回归，与三 Worker 用例连续五轮均通过（共 10 次检查）。
不改 Scheduler、CAS、租约或准入额度，不跳过失败用例。

发布注意：旧 0006 的失败逻辑调用没有 Manifest，恢复重试会以 AgentContextProjectionChanged 终止，不能补造身份。
升级前应识别这些等待重试会话并制定运维处置；正常 upgrade/备份保留已有 Manifest，downgrade 0006 会移除它。
投影可能丢失早期语义，默认 full；字节上限不是供应商精确 Token 或费用上限；完整历史仍受 8 MB 存储额度约束。
代码验证通过不代表真实 Provider、跨主机、生产部署或外部副作用 exactly-once 已获证明。

## Immutable Model Request 验收（2026-10-20）

基线 main 为 347a5af158def299cb608d5bc18e8eba83467651，其 CI 已核实成功。
契约、能力、Token 计数保证等级和升级处置见 [Model Request Contract](model-request-contract.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 完整 `uv run pytest -q` | PASS | 780 passed、320 skipped，125.39 秒；1 项已有 TestClient 弃用警告 |
| Ruff check / format --check | PASS | src tests scripts migrations evals；148 files already formatted |
| 请求身份与 HTTP 一致 | PASS | MockTransport 校验 model、max_tokens、endpoint 在预约后配置变更时仍使用冻结值；无付费请求 |
| 重试与 Session 固定配置 | PASS | 同逻辑调用 Contract/Manifest 不变；模型、Provider、输出或能力漂移在新 Attempt/Permit 前拒绝；新轮次不能静默更换 Session 配置 |
| 预检顺序与资源治理 | PASS | 字节、能力、可信计数窗口先于预算预约；配额/Observed Usage 仍生效；无可信计数的严格模式拒绝 |
| SQLite / 0008 / 只读升级核查 | PASS | 0008 → 0007 → 0008 / Alembic check；Action、审批、对账、Artifact 及旧事实不变；旧 Contract 保留 None；审计覆盖 0006/0007/0008 |
| PostgreSQL 完整发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：617 passed、零跳过，251.15 秒；含真实独立事务竞争及旧 token fencing |
| 三独立 Worker Soak | PASS | 30 秒目标，实际 34.75 秒，seed=7；23 Run、13 Action 派发、41 次领取；单机临时 PostgreSQL |
| 实际 pg_dump / pg_restore | PASS | 0008、15 表、10 Run 全量事实一致，含 Request Contract；已完成工具重派 0，明确新批准派发 1 |
| 构建 / Wheel / 架构 / 离线评估 | PASS | `uv build`；仓库外 Wheel 安装与 Durable Step；架构 3 passed；参考应用离线 3/3 |
| 本地 Docker / Compose | NOT VERIFIED | Docker daemon pipe 不可用；Sandbox 2 skipped，不计通过；推送后由本次 main CI 验证，实际链接见交付记录 |
| 真实 Provider / 真实 Token Counter / 跨宿主 | NOT VERIFIED | 未启用付费选项；无已验证 DeepSeek tokenizer 或跨宿主环境；合成 Counter 和单机 Soak 不替代真实验收 |

完整测试的 320 skipped 包括 317 项专用 PostgreSQL、2 项 Docker 和 1 项显式付费测试。
PostgreSQL 由上述独立临时实例补齐；未跳过失败检查或修改历史迁移。
新测试随原发布门禁运行，没有新增付费 CI 或第二套调度器。

旧迁移测试中“无请求身份仍可重试”的假设已更新为拒绝：用量计数和历史事实保留，预算不重置，
不能通过旧 Store 接口重新预约未知身份的失败调用。新夹具在合法完整工具映射上验证迁移，不放宽 ContextBuilder。
部署前需只读核查、停止 Writer、备份并处理旧待重试调用；0008 回退会删除契约，重新升级不能恢复已删身份。

Token 窗口保证仅限经过验证的 Counter / 上界；默认未知，不是费用硬上限。
供应商模型别名、实际权重或账单不由 Contract 证明；真实 Provider、跨宿主、长时生产负载和部署放行仍需独立验收。

## Provider Compatibility / Request Conformance v1.1（2026-10-20）

基线 main 为 0680fdc7358394dce4e1f0f21277400239387fa9。本轮不新增迁移，保留 0008 及全部历史请求字段。
官方 Chat、Models & Pricing、Change Log 和 Tokenizer 资料已在 [契约文档](model-request-contract.md) 记录查询日期、
支持名单、采样限制和保证边界；[恢复手册](production-recovery-runbook.md) 说明受控部署顺序。

| 检查 | 状态 | 实际证据 |
| --- | --- | --- |
| 新部署与生命周期 | PASS | Settings、Compose、示例环境均默认 deepseek-flash；mock 默认不变；两个规范名称允许，旧别名和未知名称在请求前拒绝 |
| 新旧接口 wire 合规 | PASS | 两个模型 × prepare/respond/complete；MockTransport 验证 HTTPS、认证 header、模型、输出、流式关闭、JSON 格式、采样、Schema 和工具 ID；不是付费 API 联调 |
| 冻结与错误分类 | PASS | 配置/上下文/Schema 热更新不改变已准备 payload；429、4xx、5xx、连接/读取超时、坏 JSON/响应单次请求并脱敏 |
| 已复现的请求选项缺口 | PASS | 先复现 context_json 夹带 temperature/thinking 与空 tools 的不同 wire，再拒绝契约外顶层键并规范空 Schema；合法 v1 摘要保持不变 |
| 旧 Session 与部署切换 | PASS | 真正 Worker/Store、历史 v1 夹具；更换新默认不改写或重试旧别名调用；当前规范模型仍以同一契约/Manifest/逻辑 ID 重试；保留非空 Usage、Permit、Action 和 Artifact |
| 只读生命周期核查 | PASS | version=2 有界聚合；旧/未知模型、旧待重试、执行中、缺身份、终态逻辑调用、去重处置数；0006/0007/0008 不写入、不输出持久身份 |
| 完整 pytest | PASS | `uv run pytest -q`：831 passed、324 skipped，129.28 秒；1 项已有 TestClient 弃用警告 |
| 真实 PostgreSQL / 历史迁移 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：625 passed、零跳过；含独立事务、CAS、fencing、取消、审批/对账及既有 Alembic 回归 |
| 静态 / 架构 / 构建 | PASS | Ruff check、format --check（150 文件）、架构 3 passed、uv build、隔离 Wheel 安装、参考应用离线 3/3 |
| 三 Worker Soak | PASS | 30 秒目标、实际 36.33 秒；seed=7、17 Run、10 Action 派发、32 次领取；单机临时 PostgreSQL |
| 逻辑备份恢复 | PASS | 0008、15 表、10 Run 全事实一致，保留 Contract/Manifest/旧审计；已完成工具重派 0，新批准派发 1 |
| 本地 Docker / Compose | NOT VERIFIED | daemon pipe 不可用；普通 pytest 的 2 项 Sandbox 跳过不计通过；本次 main CI 的实际结果由交付链接单列 |
| 真实 Provider / Token Window / 跨宿主 / 生产放行 | NOT VERIFIED | LIVE PROVIDER NOT VERIFIED；没有付费授权或目标部署演练，本轮没有真实模型请求或已验证的 DeepSeek Counter |

324 skipped 包含 321 项专用 PostgreSQL、2 项 Docker 和 1 项显式付费测试；PG 另由上述临时实例补齐。
不把跳过计为通过。旧分类测试的虚构模型名改为规范模型后仍检查相同错误，不绕过白名单或关闭检查。
本轮只对文档开展网络核实；真实 Smoke 保留显式付费选项、环境授权、专用 Key、一次请求与原超时。

严格 Token 窗口仍会在无可信计数器时拒绝；官方存在离线资源不代表已验证本项目完整请求模板。
请求契约不证明供应商权重、默认 thinking 行为、采样实际生效、真实费用上限或 exactly-once 账单。
