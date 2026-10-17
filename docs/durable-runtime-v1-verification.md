# Durable Agent Runtime v1 验收矩阵

基线 main `fe18641` 包含已合并 PR #7；[基线 CI](https://github.com/L-305-maker/Altas/actions/runs/38990091222) 五个作业成功。
本阶段不修改 Domain、Runner 状态机、DAG 或 0001–0003。以下是具体故障契约，不代表穷尽所有交错。

测试缩写：[crash](../tests/chaos/test_durable_runner_crash.py)、
[commit](../tests/integration/test_runtime_commit_faults.py)、
[chain](../tests/integration/test_runtime_hardening.py)、
[store](../tests/integration/test_agent_store.py)、
[cancel](../tests/integration/test_agent_cancellation.py)、
[review](../tests/integration/test_action_review_runner.py)、
[operators](../tests/integration/test_action_reviews.py)、
[governance](../tests/integration/test_model_governance.py)。

| 故障窗口 | 测试 / 参数 | 持久状态及恢复契约 |
| --- | --- | --- |
| 1. 模型预约 COMMIT 前退出 | crash before_model_reservation（新增） | 在数据库 commit hook 真正退出，预约回滚、预算 0 / revision 0；租约过期后新 Worker 正常预约 |
| 2. 预约成功、发请求前退出 | crash model_reservation（已有） | MODEL_INFLIGHT 无法确认是否已发请求，记录模型 UNKNOWN 并 fail-closed，不自动重发 |
| 3. 模型已返回、响应保存失败 | commit response_rollback / response_lost_ack（新增） | 回滚保留 MODEL_INFLIGHT，恢复按 UNKNOWN 终止；若实际提交但确认丢失，则复用已存 Action，不重发该轮 |
| 4. 延期事务提交后退出 | crash retry_scheduled（已有）；commit retry_lost_ack（新增） | 保留 available_at、模型 ID、预算和继续标记，到期才领取 |
| 5. Action 预约后、派发前退出 | crash tool_reservation（新增）；store 原有预约恢复 | 外部调用次数 0，仍保守记录 UNKNOWN / WAITING_RECONCILIATION，不能靠猜测重派 |
| 6. 工具执行后、结果保存前退出 | crash tool_dispatch（已有） | 工具只执行一次，结果转 UNKNOWN / Step waiting，必须人工核对外部事实 |
| 7. 工具结果保存后、Worker 返回前退出 | crash tool_result（已有）；commit tool_lost_ack（新增） | 已存结果、Action ID 和幂等键不变，新 Worker 复用观察 |
| 8. 批准后、重新领取前取消 | chain cancel_after_approval（新增） | 无工具派发，下游不成功，旧 token 读写均 LostLease |
| 9. 批准消耗后工具 UNKNOWN | review test_consumed_approval_survives_cancel_and_unknown_is_not_reexecuted；chain complete（新增组合） | 保留消耗事实，UNKNOWN 不重发；对账后只回放最终有效观察 |
| 10. 对账 / 取消 / claim 并发 | chain cancel_reconciliation_race（三个独立事务，新增）；operators 原有双事务竞争 | 对账先完成或因取消而冲突；取消后的新证据仅追加审计。最终仍取消，晚到 Task 被 fencing，下游不成功 |
| 11. 延期到期与取消并发 | chain cancel_due_retry（新增）；governance 原有安排/取消竞争 | 即使先领到 Task，取消后仍无执行权，不再次调用模型 |
| 12. 终态 / Step / Artifact 提交故障 | commit terminal_rollback / terminal_lost_ack（新增）；store 插入回滚 | 回滚三者都不成功；实际提交后丢失确认仍只有一个 Artifact |

已有预算、Action 顺序、UNKNOWN 禁止重试、容量预留、错误脱敏及终态审计继续回归，不复制等价用例。
commit 同时覆盖审批/对账连接故障的回滚与提交后确认丢失；精确重试不新增决定或审计。

## 不变量与组合执行

[runtime_assertions.py](../tests/runtime_assertions.py) 按 Run→Step→Session→审批→ModelCall→Action
读取检查点，不修补数据。检查 revision/预算单调和上限、模型 ID/轮次、追加消息、Action 元数据/参数/
幂等键稳定、完成行动不变、UNKNOWN 只能经正式对账解决、等待状态无 INFLIGHT、Step 等待/终态一致、
延期 READY_MODEL/失败记录/available_at/继续标记、Artifact 唯一。旧 Task 的 load 和 commit_transition
均必须 LostLease，不能用普通状态冲突代替所有权检查。

两个独立 Store/连接池及 Worker.tick 执行：审批等待 → 另一个就绪 Step 成功 → 工具 UNKNOWN →
阻止下游 → 人工对账 → 模型延期 → 另一个 Workflow 成功 → 并发领取同一 Agent Step → 成功 →
下游仅执行一次。max_attempts=1 仍可正常审批恢复及模型内部重试，预算和身份不重置。
真正失败的有界重试复用 operators/cancel 现有用例。跨 Run 隔离通过完整持久 Run 状态前后比较验证。

竞争仅在专用 PostgreSQL 执行，连接设置 2 秒 lock_timeout、5 秒 statement_timeout；
Barrier/future/asyncio 均有超时，SQLite 不承担行锁证据。shutdown 同时保留 waiting、ready-delayed、
running 三个 Session：原取消传播，等待和延期不被改写，进行中模型未知结果安全保存。
五秒清理、二次取消、迟到线程和新 Worker fencing 复用 cancel；不声称可强制终止驱动同步线程。

## 运行指标

复用 [Prometheus 模块](../src/agentflow/infrastructure/telemetry/metrics.py)，无新 exporter 服务。
默认关闭；API/CLI 仍由 AGENTFLOW_TELEMETRY 开启，/metrics 仍要求 operator Bearer 认证。
Store 可显式注入 RuntimeMetrics，Worker 只接收可选耗时回调，不依赖 Prometheus。

| 指标 | 有限 labels / 语义 |
| --- | --- |
| agentflow_agent_transitions_total | state：Domain 固定状态；确认 COMMIT 后计数 |
| agentflow_agent_model_requests_total | outcome=reserved/succeeded/failed/unknown，预约或结果写入，不能解释为计费 |
| agentflow_agent_model_retries_total | 无 labels，已提交延期事务 |
| agentflow_agent_action_reservations_total | 无 labels，已提交预约数，不能解释为外部 exactly-once |
| agentflow_agent_reviews_total | kind=approval/reconciliation；outcome=requested/approved/rejected/succeeded/failed/audit_succeeded/audit_failed 的固定组合 |
| agentflow_runtime_failures_total | kind=lost_lease/state_conflict/database/lease_expired/context_limit，故障信号而非状态成功 |
| agentflow_store_transaction_seconds | outcome=committed/error，含读事务；error 不证明回滚，提交结果也可能未知 |
| agentflow_worker_execution_seconds | outcome=returned/cancelled/stopped/error，不推断 Step 成败 |

耗时固定 buckets、采样截在 60 秒，非有限值丢弃；HTTP method/status 也归一化到有限集合。
禁止 Run/Agent/Action ID、工具名、Prompt、参数、actor、证据、异常正文进入 labels。
事务中只排队元数据，确认 COMMIT 后采集；回滚不计成功，幂等决定不重复计数。
初始化、观察和 HTTP 生成失败不改变业务结果；指标不可用时认证请求安全返回 503。
同一个 Store 可组合多个 API 实例而不重复注册 HTTP collector。

默认指标进程本地，重启重置，不能将 API 计数冒充独立 Worker 统计。部署可选用已有的
PROMETHEUS_MULTIPROC_DIR：启动 Python **之前**设置，同一部署 API/Worker 共享受保护可写目录/卷，
从原认证入口收集。整个部署停止后才能为新部署清理旧文件，不能清理活动目录。
两个真实子进程验证 Worker 写入→API 认证收集；没有新增未认证 Worker HTTP 端口。
不用共享目录需分别收集进程，不声称跨主机聚合，本阶段没有分布式指标存储。

计数是 best-effort 运维信号：提交后采集前退出、COMMIT 确认丢失或指标故障可能少计。
不能从指标恢复 Agent，不作为费用/token 账本。持久事实只来自 AgentStore 与审计。

## 数据库、发布与部署边界

已有 SQLite/PostgreSQL 0001→0002→0003 迁移回归保留 Workflow/Step 和历史 Agent 数据。
[新发布迁移测试](../tests/integration/test_runtime_release_migrations.py) 从空库 upgrade head，
创建可验证的批准消耗、UNKNOWN 对账及观察，再正常 upgrade head / alembic check 并恢复执行。
本阶段 head 仍为 0003，这是同版非破坏检查，不宣称验证尚不存在的未来迁移。
关闭 telemetry 时，指标模块不可导入也不妨碍旧数据库和普通 API 启动。

共享 PostgreSQL，独立 Worker 连接池，租约使用数据库时钟。部署须设置连接/语句超时，使最坏数据库
等待与心跳间隔匹配租约窗口；协程不能强制终止驱动线程。审批绑定名称、版本、Schema/副作用策略和
参数摘要；修改实现必须更新工具版本。operator token 是可信单租户操作员边界，不是多用户 RBAC。
保护数据库、共享指标目录和凭据，指标不输出敏感输入。

仅明确确认的模型失败自动延期；内部模型尝试不消耗虚假的 Step 失败预算。INFLIGHT 崩溃/工具结果未知
必须人工对账，预约后未派发也不能推测无副作用；模型 UNKNOWN fail-closed 且可能已计费。
上下文字节硬上限不是 token/费用上限，数据库与外部副作用不可原子化，不保证外部 exactly-once。

CI 复用原工作流，新增 PostgreSQL 发布门禁仅在 Python 3.13 开启，避免新建大型重复矩阵。
临时 PostgreSQL 测试最长 600 秒，结束后关闭删除隔离实例；Python job 最长 20 分钟。
全量 pytest、架构、Ruff、wheel/sdist、仓库外安装、Docker/Compose 与参考应用继续执行。

## 本次实际验证

| 实际命令 | 本地结果 |
| --- | --- |
| uv run pytest -q | 590 passed，196 skipped；1 项已有 Starlette TestClient 弃用警告 |
| uv run ruff check src tests scripts migrations evals | 通过 |
| uv run ruff format --check src tests scripts migrations evals | 106 files already formatted |
| uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates | 380 passed，零跳过；独立真实 PG，含双后端对照和发布迁移 |
| uv run pytest -q tests/unit/test_architecture_boundaries.py | 3 passed |
| uv build | wheel/sdist 成功 |
| uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl | 仓库外安装，参考应用、Durable Runtime、认证指标及 3 项离线评估通过 |
| docker info --format '{{.ServerVersion}}' | 本地 Docker Desktop Linux daemon 不可用，本地 Docker/Compose 未验证 |

全量跳过来自未配置专用 PostgreSQL 或 Docker 环境，不能算通过；PG 成功来自单独的真实临时实例。
事务耗时异常标签最后改为 error，避免暗示未知提交已回滚；随后指标回归为 7 passed、5 skipped。
提交后的完整 CI、Docker/Compose 和参考应用结果见本 Draft PR 检查及
[该分支 Actions](https://github.com/L-305-maker/Altas/actions?query=branch%3Afeat%2Fdurable-agent-v1-hardening)。
MockProvider/MockTransport 不代表真实模型 API 联调；本轮没有调用真实模型 API。
