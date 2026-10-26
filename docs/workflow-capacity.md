# Workflow Capacity Admission v1

## 四种控制

| 控制 | 决定什么 | 权威事实 |
| --- | --- | --- |
| Workflow Admission | 是否创建新的活跃 Run | 数据库中非终态 Run |
| Worker concurrency | 单个 Worker 同时执行多少 Handler | 该 Worker 的执行槽位 |
| Provider Admission | 特定模型请求能否预约 Permit | 既有策略、Permit 与 Attempt |
| Resource accounting | 实际记录了多少用量 | 持久 Usage；本轮不实现费用结算 |

准入不限制已有 Run 的领取、审批、UNKNOWN 核对或延期恢复，不预约 Provider Permit。
Worker concurrency 不是活跃 Run 上限，Run 上限也不是模型吞吐或 Token / 费用保证。

## 配置与混部

默认 `WorkflowAdmissionPolicy(policy_version="v1", max_active_runs=None)`，保持历史提交行为，
不增加提交锁。可信部署环境可配置 JSON 值：

```text
AGENTFLOW_WORKFLOW_ADMISSION={"policy_version":"v1","max_active_runs":24}
```

这不是 Workflow 参数。限额必须是正整数或 null；布尔值、字符串数字、零和负数拒绝。
公共 Facade 导出 `WorkflowAdmissionPolicy`、`WorkflowCapacityExceeded`；Python Consumer
组合 `Store(url, workflow_admission=policy)`。API 自建 Store 与 CLI Worker 读取 Settings；
注入 API 的 Store 由调用方配置，Settings 不会覆盖它。

容量统计排除既有 TERMINAL 集合 succeeded / failed / cancelled，计入全部其他 Run。
当前 running 和 waiting 均占容量，包括 Step / Action 审批、工具 UNKNOWN 等待核对及延期重试。
真实 Run 终态提交才释放容量；不建立第二个计数账本，不修改已有 Run 来腾出空间。

同一数据库的全部新 Run Writer 必须使用相同启用策略与限额。旧代码、关闭策略的 Store、
不同限额或直接 SQL Writer 可以绕过合作协议，混部时不能宣称全局硬上限。
滚动升级先保持关闭，确认所有 Writer 支持后统一启用；降级前关闭策略。
降低限额不取消既有 Run。旧 Workflow 定义、Run 快照和 Session 请求身份不改写。

## 原子性与 HTTP 契约

受控 submit 的同一事务：独立 advisory transaction lock → 全局幂等身份检查 → Workflow
校验 → COUNT 非终态 Run → 创建 Run / Step / Event / Approval → COMMIT。
PostgreSQL 锁为两整数 `(1095128897, 1)`，表示 ASCII AFWA 命名空间与协议 v1。
它仅协调当前数据库内遵守协议的提交者，提交 / 回滚时释放，不复用 RC Worker 的 session lock。
要求 READ COMMITTED，使锁等待后的 COUNT 获得新已提交快照；其他隔离级别拒绝受控提交。
启用策略的事务设置 5 秒 statement / lock timeout。DB 故障或超时不会解释为空闲容量。
SQLite 复用 BEGIN IMMEDIATE，只保证现有本机语义，不替代独立进程 PG 验证。

相同 submission_key 和相同请求即使满额也返回原 Run；不同请求保持 409。
key 仍全局唯一，不按 Workflow 分组。COMMIT 确认丢失后用原 key / 请求重试；
没有 key 的提交无法消除确认丢失后的重复创建。失败事务不产生部分行或虚假容量占用。

容量拒绝为 HTTP 429 与 `{"detail":"WorkflowCapacityExceeded"}`；没有可证明恢复时间，
不返回 Retry-After。冲突仍为 409，数据库不可用 / 查询超时仍为安全的 503。
客户端应有界退避并保留原 key，不要因 503 换 key 重建。拒绝不消耗 Step Attempt、预算或 Permit。
没有 Schema / 索引变更；Alembic Head 仍为 0008。

## 有界只读诊断

认证 `GET /api/runtime/queue` 返回固定大小聚合：active_runs、ready_steps、delayed_steps、
leased_steps（包含过期）、expired_leases、waiting_approval、waiting_reconciliation、
oldest_ready_wait_seconds、ready_age_fallback_steps、本 Store 的策略与可选拒绝观测数。
waiting 字段按 Step 计数；只统计非终态 Run 的 Step，ready / delayed 以 available_at 区分。
不返回 Run / Step ID、Lease token、输入、Prompt 或工具结果。

范围为数据库保留记录的全量一致性快照，无时间窗口、不分页、不返回原始行。
聚合可能扫描大量数据，但结果大小固定，超时后直接报错，不返回伪装为全局总数的部分扫描。
PostgreSQL 使用 REPEATABLE READ / READ ONLY，默认 5 秒 statement timeout、2 秒 lock timeout；
Python 入口允许 1–60 秒。SQLite 使用只读事务和 progress deadline。连接 / 池等待受既有部署配置约束。
`scope.as_of` 记录观测数据库时间。已有细粒度审计的 since / limit / offset / after-step 不变；
`audit_durable_runtime.py --queue --timeout-seconds 5` 复用聚合查询，省略部署策略与进程 Counter。

ready 等待起点取 available_at 与最后 step.ready / approval.approved 事件时间的较晚者；
缺少事件则回退 Run.created_at，并报告 fallback 数。没有 ready Step 时年龄为 null。
旧事件 / Run 时间由客户端时钟生成，不能作为跨主机时钟偏移下的精确 SLA 证据；
资格、租约、Fencing、CAS、取消仍保持现有数据库时间协议。

启用 RuntimeMetrics 后增加无 label 的 `agentflow_workflow_admission_rejections_total`。
admission_rejections_observed 是当前进程可丢失的观测；未启用或读取失败为 null，
不是持久拒绝总数或配额事实。多进程汇总沿用既有 Prometheus multiprocess 配置。
遥测失败不改变事务结果；容量权威来源始终是数据库 Run 状态。

## 基准与公平性边界

`scripts/benchmark_workflow_capacity.py --bin <本机 PG bin> --samples 5` 只创建自有临时 PG，
记录 Python / OS / PG / CPU、样本、负载、代码 SHA 和文件哈希，不接受外部数据库 URL。
3 / 100 / 500 个根 Step × 1 / 24 / 96 活跃 Run；4 个独立 Engine；旧大 DAG 与持续提交短 Run；
真实 Step / Action 审批、工具 UNKNOWN、延期重试、租约过期 / 旧写拒绝与取消；实际 Worker 并发峰值。
输出 submit / claim 分位数、领取速率、根 Step 与最老可领取根 Step 等待、查询计划、锁等待采样，
以及 opt-in 提交 / 拒绝 / 诊断耗时。20 毫秒采样为 0 不证明短暂锁争用不存在。

现有调度先耗尽旧 Run 的持续 ready Step，再服务后来短 Run，没有轮转公平性保证；
独立 Engine 通过 SKIP LOCKED 跳过已锁 Run。本轮不改变 claim 选择顺序、锁或执行语义。
观察到有限队头延迟，但没有可接受延迟 SLO 或更大负载证据支持重写；这不等于公平性问题已解决。
后续改动需先定义 SLO、测量并发影响，再评估最小调度调整和索引，不能据小样本承诺生产吞吐。

独立 installed-wheel Consumer 显式限额 2，覆盖满额 429、幂等重放、冲突、取消释放及既有
Durable 审批 / UNKNOWN / 重启 / Fencing 路径；它只导入公共 Facade。
没有成本 / 资源单位记账、Planner、RAG、Memory、Multi-Agent 或 Multi-Tenant。
真实双宿主、DeepSeek、Token Conformance、HA/PITR 和生产发布仍为 NOT_VERIFIED。
