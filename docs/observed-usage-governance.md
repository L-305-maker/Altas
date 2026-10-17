# Observed Usage Governance v1

本能力只决定是否允许**下一次**模型预约。它读取已持久化的供应商报告，不预估本次请求的最大 token，
不确认供应商账单，也不把 token 换算成货币。一次合法响应可以使观察值超过阈值，仍须完整保存。

## 策略与作用范围

`ObservedUsagePolicy` 是 frozen / extra=forbid 的类型：

- `max_observed_input_tokens`、`max_observed_output_tokens`、`max_observed_total_tokens`：严格非负整数或 None。
- `missing_usage_policy`：默认 `fail_closed`，也可显式选择 `continue_with_warning`。
- `policy_version`：必填的稳定非敏感版本标签。

None 表示该轴未限制；0 禁止第一次预约。正阈值在已知、版本化映射值之和 **>=** 上限时拒绝新预约。
空账本不是一次缺失报告，正阈值允许首次请求。观察累计按 AgentSession，**不是整个 Run 的共享额度**。

可信 Workflow 在冻结的 `StepSpec.parameters["agent"]["observed_usage"]` 中设置策略，例如：

```json
{"mode":"durable_v1","observed_usage":{"max_observed_total_tokens":10000,"missing_usage_policy":"fail_closed","policy_version":"1"}}
```

部署可用 `Settings.observed_usage` 或环境变量 `AGENTFLOW_OBSERVED_USAGE` 提供同型 JSON：

```sh
AGENTFLOW_OBSERVED_USAGE='{"max_observed_input_tokens":20000,"max_observed_total_tokens":30000,"missing_usage_policy":"fail_closed","policy_version":"prod-1"}'
```

两者同时存在时，每轴取所有已设置阈值的最小值，缺失策略取更严格的 fail_closed。
组合策略的 `policy_version=intersection-v1` 表示组合规则；原始版本完整保存在 Run 绑定及冻结 Workflow 中。
普通 Run.inputs、依赖数据、Prompt、工具参数和模型响应不能提高这些阈值。
未启用策略时保留原有输出和执行路径；`max_resource_units=0` 的兼容要求不变。

Run 首次打开 Durable Agent 时，在已有 Run 锁内冻结部署策略和允许的映射定义。
后续 Agent Step 同样使用这一绑定；Workflow 的每 Step 策略来自持久 Run.spec。
重启、接管或不同 Worker 使用不同配置时，open / 新预约会 StateConflict 安全停止，不能悄悄重设额度。
这是首次 Agent 使用时绑定，不是 submit 时绑定；升级前已有 Run 在首次接入时冻结配置。
运行中的 Run 必须保留原配置供合法恢复，新配置用于新 Run；本版没有在线修改绑定的接口。

## 映射来源与版本

`UsageMapping` 保存 provider、version、input_field、output_field、total_field；不执行任意代码或推断。
`UsageMappingRegistry` 仅在可信 Python 组合入口注册，初始化后不可变，没有进程全局可变注册表。
默认注册：

| 来源 / 版本 | 原始字段 → 标准字段 |
| --- | --- |
| generic / 1 | input_tokens、output_tokens、total_tokens 同名映射 |
| deepseek / 1 | prompt_tokens → input_tokens；completion_tokens → output_tokens；total_tokens 同名 |

generic 是供应商无关的标准字段契约，不声称识别了真实供应商账户。
DeepSeek 适配器固定使用 deepseek/1，不从响应正文选择映射；原 `complete()` 行为保留。
自定义字段通过显式注册的映射生成 `normalized_usage`：

```python
definition = UsageMapping(provider="custom", version="1", input_field="in", output_field="out")
registry = UsageMappingRegistry((definition,))
agents = SQLAgentStore(store, usage_mappings=registry)
runner = DurableAgentRunner(agents, provider, tools, usage_mappings=registry)
# Provider adapter: normalized_usage=definition.normalize(raw_usage)
```

启用 Provider Admission 时照旧将 `admission=agents` 传入 Runner，不能省略或绕过准入。
Runner 和 Store 都核对 raw 与 normalized 的一致性，Store 额外核对注册及 Run 绑定。
未注册映射、同一版本规则漂移、伪造 normalized 数字都拒绝。字段规则变更必须发布新版本，不能重新解释旧记录。
注册表只表示可信部署者认可的适配契约，不是供应商认证或计费审计。

新确认用量把完整映射定义存入原有 Attempt.usage JSON。恢复按记录自己的冻结规则重验 raw，不依赖当前
Provider 注册，因此普通 Store 取消、租约恢复和只读查询也能处理自定义映射历史。
旧记录没有映射版本时，不回填版本、不重写原始或规范化数字。普通 `usage_summary()` 仍返回这些旧事实；
治理报告单独排除无版本数字，并将该字段计为未覆盖。这两种汇总不能混称为完整账单。

## 覆盖率与未知消耗

三个轴独立判断；input + output 不替代未报告的 total，total 也不反推其他轴。
合法字段为严格非负整数；无效单项保留原始有限 JSON，但该规范字段为 None。缺失不写成零。

| 尝试事实 | 治理中的处理 |
| --- | --- |
| 已确认且有合法版本化报告 | 对每个明确报告的字段累加 |
| 已确认成功但无 usage | 所有字段未覆盖 |
| 已确认失败 / 429 且无 usage | 保留 FAILED、退避与冷却；下一次预约按未覆盖处理 |
| 部分字段报告 | 仅累加这些字段，其他字段保持 None / 未覆盖 |
| UNKNOWN / INFLIGHT | 未覆盖，不造用量；UNKNOWN 原本就不自动重发 |
| LEGACY_UNMEASURED / 无版本历史数字 | 保留原事实，治理不把它当成已验证映射值 |

fail_closed 只因**设有限额的轴**覆盖不足而拒绝；未限制轴的缺失仍在报告中显示。
continue_with_warning 在已知部分未达阈值时可继续，绝不能据此声称完整消耗低于上限。
`AgentSession.observed_usage` 包含每字段部分和、reported/unreported_attempts、未知及旧记录数量、
unversioned_attempts、partial 和 blocked。没有任何该字段测量时 value=None。
partial 表示任意轴存在覆盖不足；blocked 表示“下一次预约会被拒绝”，不是修改当前终态。

启用策略的成功 Step / Artifact 增加 `observed_usage` 元数据，保留原 text 等结果；非对象结果放入 value。
元数据由 Store 根据提交后的账本生成，不接受 Provider 覆盖。即使当前成功响应使 blocked=true，
本次 SUCCEEDED、usage、Action 与 Artifact 仍作为合法事实保存；Step.error 不因此改成失败。
未启用策略时不增加该输出字段。

## 事务与恢复

执行顺序：Context Validation → AgentBudget Validation → Observed Usage Governance → Provider Admission
→ ModelAttempt Reservation → Provider HTTP。观察阈值与硬调用预算、全局请求容量分别管理。

在 Run → Step → Session → ModelCall → Attempt → Action 的既有锁与数据库时钟租约下恢复账本，
先校验 `advance()` / Revision CAS，再判断当前汇总，之后才锁 Provider 行。普通预约与带准入的预约
都执行此检查，不能用 commit_transition 绕过。配额延期仍不消耗模型预算或新尝试。

拒绝时预约事务回滚，抛出安全的 `ObservedUsageBlocked`；不申请 Permit、不增加任何调用计数。
Runner 用原 token 与原 revision 通过 fail_agent_step 原子提交 Agent EXHAUSTED 和 Step failed：
阈值已达为 AgentObservedUsageLimit，所需覆盖不足为 AgentUsageUnavailable。
这一终态提交仍受 CAS / 租约保护；间隙发生取消、接管或冲突时不能覆盖新状态，不能伪造提交成功。

已发请求的结果提交不执行阈值拒绝。合法结果与账本、Snapshot、Action / Artifact 保持原事务；
数据库回滚不产生虚假已知用量，COMMIT 确认丢失后只读已提交事实。已有 Action、观察和审计不删除。
本策略不新增工具权限，也不改变已提案工具的审批/执行规则。
取消优先、原 token fencing、五秒协程清理上限、UNKNOWN 人工对账规则保持不变。

## 迁移与部署

0006（down_revision=0005）只新增可空 `runs.usage_governance` JSON，保存首次绑定的策略和映射定义。
映射元数据复用现有 usage JSON，不增加第二本账或新表，不修改 0001–0005，也不重写旧尝试。
SQLite 与 PostgreSQL 都使用 Alembic upgrade head；create_schema 不能代替已有数据库升级。
当前代码只在当前 schema 上运行，不能用当前完整 ORM 验证回退库不存在的新字段。

升级应先停 Writer、备份、upgrade head，再以一致配置启动 Worker。正常升级和备份恢复保留 Run 绑定。
显式回退到 0005 会删除绑定元数据，不能声称活跃 Run 的部署策略无损回退；恢复运行前须从同版本备份
恢复绑定及原配置，或在受控维护中终止旧运行。不要用回退再升级来放宽额度。

不新增公开管理接口、遥测标签或网络依赖。EventRow、Prometheus labels 和普通日志不包含原始 usage、
request_id、响应正文、Prompt 或凭据；它们仍仅存在于受保护的持久记录中。
真实 DeepSeek 与跨主机网络故障演练不由本任务自动执行。

## 验证入口

新增 `test_observed_usage.py`、`test_observed_usage_governance.py`、`test_observed_usage_migrations.py`、
`test_observed_usage_crash.py`。回归包含阈值边界、覆盖不足、映射冲突/漂移、普通取消、自定义映射恢复、
独立 PostgreSQL 事务竞争、结果确认丢失、真实进程退出及审批/UNKNOWN 对账。
测试复用真实 claim 和共享不变量；没有通过补写用量、重放 UNKNOWN 或放宽 fencing 掩盖失败。

现有 PostgreSQL 发布专项已包含新增测试；备份演练同时启用准入和 continue_with_warning 策略，
比较全部表与绑定，并用三个独立 Worker 恢复。Wheel smoke 也启用观察策略；默认 Soak 验证关闭策略的兼容路径。
### 本次本地结果（2026-10-17）

Windows / Python 3.12，真实 PostgreSQL binaries：`D:/PSQL/bin`。

| 命令 / 验证 | 实际结果 |
| --- | --- |
| `uv run pytest -q -rs` | 711 passed、275 skipped；跳过不算通过 |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 530 passed；真实独立临时实例和事务，包含 0006 迁移、并发及进程崩溃 |
| `uv run ruff check src tests scripts migrations evals` | PASS |
| `uv run ruff format --check src tests scripts migrations evals` | PASS，135 files already formatted |
| `uv run pytest -q tests/unit/test_architecture_boundaries.py` | 3 passed；全量也包含该检查 |
| `uv build` | PASS，sdist / wheel |
| `uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` | PASS；仓库外安装、启用观察策略与准入、参考应用和认证 metrics |
| `uv run python evals/run.py` | offline 3/3 |
| `uv run python scripts/verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；0006、15 表、10 Runs；原策略、映射和准入事实恢复一致，已执行工具重放 0 |
| `uv run python scripts/verify_durable_soak.py --bin D:/PSQL/bin --seconds 30 --workload 6 --seed 7` | PASS；35.78 秒、23 Runs、三个独立 Worker 进程 |

275 个全量跳过中，272 项因未配置专用 PostgreSQL URL 而跳过（已在独立实例专项实际执行），
两项 Docker opt-in 未启用，一项 live-provider 因显式付费开关关闭而跳过。
已有 Starlette/httpx 弃用警告保留。本地 Docker 引擎不可用，Sandbox / Compose 本地 NOT VERIFIED；
由既有 GitHub CI 实际验证，不能用跳过冒充通过。真实付费模型及跨主机演练 NOT VERIFIED。
本文件记录本地证据；精确提交的 GitHub Actions 状态另见交付报告与对应 main 运行记录。
