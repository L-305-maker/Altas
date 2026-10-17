# Durable Runtime v1：Release Candidate 验收

## 结论与基线

本 PR 基于 PR #8 HEAD `4510f775f4b812042e67afeeaed3acb3fa375d46`，上游仍为未合并 Draft。
堆叠 base 为 feat/durable-agent-v1-hardening；合并上游后应重新定位 main 并重跑 CI。
本页记录 2026-10-17 的实际证据，后续部署必须重做环境项。

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

204 skipped 包含专用 PG、Docker 和 1 项显式 live 门控，不计通过。真实 PostgreSQL 由独立临时实例
专项补充。本分支 GitHub CI 的实际结果在 PR 中记录；本地 Docker 不可用不借用上游 CI 的成功替代。
MockProvider/MockTransport 不是供应商实测。完整故障窗口复用 [v1 verification](durable-runtime-v1-verification.md)。

新增验证先执行失败回归：审计入口缺失、隔离资源入口缺失、live 双重授权及 Durable harness 缺失；
再实现入口。实际演练也发现测试断言遗漏“崩溃对账后真实有界 Step 重试”的 ready 状态，以及被取消
前尚未创建 Session 的 Task。仅修正测试检查器/验证器，不修改 Domain 或运行时状态规则。

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
