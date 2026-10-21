# Durable Runtime v1：备份、恢复及 staging 演练

## Provider Egress 发布检查

策略/兼容矩阵见 [Provider Context Egress](provider-context-egress.md)。0008 schema 不变；旧 Attempt 的
Manifest/Contract 无新字段，新请求包含 egress/context_egress_digest。备份恢复在源与恢复库逐事实比对
这些身份；演练新工作负载显式 metadata_only + 等价 v3，旧 v1/v2 终态不补填权限或新字段。
上线前停止所有旧 Writer/Worker，统一部署能解析新 JSON 的版本，再恢复；不能混跑旧二进制并假设它会过滤。
不要修改旧 RunSpec/绑定/Attempt 为重试“升级权限”；无法证明相同视图的重试应停止并人工处置。
恢复库仍禁止自动重放 UNKNOWN、重用消费的审批或重新派发已完成 Action。核查未配置策略的旧 Run：
full/recent 仍会完整外发结果，不能把本次显式功能宣称为所有历史任务自动脱敏。

这些入口使用通用测试工具，不安装到业务运行入口，不改变 Agent 审批策略。逻辑备份恢复不证明
PostgreSQL 主从切换、网络分区容错、PITR 或零数据丢失。生产数据恢复必须由有权限的操作者执行。

## 本地隔离演练

从仓库根目录运行（Windows 可使用 `D:/PSQL/bin`）：

```sh
uv run python scripts/verify_runtime_backup.py --bin <PostgreSQL-bin>
uv run python scripts/verify_durable_soak.py --bin <PostgreSQL-bin> --seconds 180 --workload 6 --seed 7
```

两个入口均自行 initdb，只监听 loopback 随机端口；trust 仅限临时实例。它们不接受外部 URL、
数据库名或删除目标；仅在 OS 临时目录创建 `agentflow-rc-*`，仅允许 source/restored 两个固定库名。
即使环境中已有生产数据库 URL，迁移也只连接脚本创建的库。子进程与 pg_ctl 有超时，finally 关闭
Worker 及连接、停止临时实例并删除合成备份。Windows 关闭的是自身创建的进程树，包含 venv 启动器。
不要用生产数据替换测试夹具。失败不代表自动修复；进程强杀、主机断电等仍可能需要检查残留临时资源。

备份演练先停止 Writer，保留已完成结果、待审批与已消耗审批、已应用对账、未解决 UNKNOWN、未来重试、
取消 Run。实际执行 custom-format `pg_dump`，向新库 `pg_restore --exit-on-error`，比对全部业务表
以及 Alembic 0008。比对包括逐尝试 Request Contract、Context Projection Manifest、观察策略/映射的 Run 绑定、Provider 准入策略/许可/429 冷却/延期、
逐次模型尝试/用量、消息、revision、
预算、模型 ID、Action/幂等键/绑定、审批/审计、时间和
Artifact 内容/digest；内容仅在进程内比较，不写报告。恢复 Worker 不重派已执行工具；待审批行动
在恢复后明确批准才首次执行；未解决
UNKNOWN 经显式操作员对账后继续，延迟重试按原数据库 deadline 自然到期。

稳定性入口先跑确定性的故障握手，再持续多个 DAG 波次。每个 Worker 有独立 Engine/连接池；
seed 固定输入顺序和故障阶段，**不承诺 OS 竞争的逐条交错或随机 UUID 可重现**。
`--workload` 是每波 Run 数（1–1000），`--seconds` 为目标运行时间（1–3600）；最后一波可超出目标。
每个进展等待最多 20 秒，整轮进展等待受目标时间加 120 秒约束，数据库有 5 秒 statement / 2 秒 lock
上限。故障包含 Worker 强杀接管、审批后所有原进程退出、延期前重启、已派发 INFLIGHT 崩溃、
UNKNOWN 对账、取消/接管竞争，以及实际停止并重启专用 PostgreSQL。连接不可用时允许保守停止，
不会把它解释为外部执行失败。复用 `tests/runtime_assertions.py`，只读核对状态，不修补数据。

普通 CI 只跑 30 秒目标的有界样本及备份恢复一次。长测试使用 `Manual durable runtime RC acceptance`
手动工作流或以上命令，最长手动门禁 40 分钟；不把 PR CI 变成长时间负载测试。

### 诊断数据最小化

Worker 的 claim JSONL 仅保留 run_id、step_id、原 token、attempt；不序列化 Task、spec、inputs、
dependencies、Prompt、工具参数或 Secret。派发只记录 Action ID 与幂等键，原有稳定性检查继续生效。
旧 Task 的不可变 StepSpec 在核查时从隔离数据库取回，仅在内存使用；fencing 不需要的 inputs 和
dependencies 保持空对象。真实旧 token 仍用于 load/commit_transition 的 LostLease 验证，不生成替代 token。

每个 Worker 在记录根目录内新建独立私有子目录：POSIX 为 0700，JSONL 为 0600；Windows 使用
Python 的私有目录 ACL，只允许当前用户与管理员访问。Windows 运维脚本要求 Python 3.12.4+，
旧补丁版本拒绝执行，依据 [Python mkdir 权限约定](https://docs.python.org/3.12/library/os.html#os.mkdir)。
包含旧 token 的目录只用于私有测试，不得贴入普通日志、复制为公开诊断或上传 CI Artifact；结束后按
测试资源策略清理。公开执行报告只保留聚合结果，Token 和数据库内容不进入报告。
如曾使用旧 staging 入口，原 JSONL 可能包含完整 Task，应按敏感数据检查并受控清理；升级不会自动
删除操作者已有文件，也不能撤销此前已经复制出去的数据。

## 生产备份与恢复操作顺序

1. 明确恢复点、允许的 RPO/RTO、数据库和应用版本，记录授权。先在隔离环境验证备份可读。
2. 关闭新提交和操作员审批/对账入口，停止所有 Writer/Worker（包括其他主机、旧版本和自动重启服务）。
   轮询确认没有新心跳或提交。不要只停止 API；运行中的外部工具可能已产生效果。
3. 由最小权限备份角色运行经审核的 `pg_dump`。凭据通过受保护环境、服务凭据或权限受限的 passfile
   提供；不要在命令参数、普通日志或 CI Artifact 中保存密码/URL。备份包含 Prompt/工具结果/操作员
   证据，应加密、限制访问、建立独立保管和到期删除策略；保留期与业务审计/恢复要求一致。
4. 恢复到新隔离库，**不要默认覆盖或删除原库**。使用对应 PostgreSQL 工具版本，检查命令退出码，
   比对迁移版本、记录数和关键 digest，运行只读审计。0001–0005 不改写；升级另行审批。
5. 对恢复点之后可能已经发生的外部操作进行盘点。备份之后的已完成调用可能在旧数据库中存在、
   在恢复库中不存在；逻辑恢复不能重建这些外部事实。保持稳定幂等键，UNKNOWN 必须人工核查。
6. 隔离旧数据库/旧 Worker，使其不能继续提交。不要为了让旧 Task 成功而替换 token 或 revision。
7. 验证恢复库权限、连接/语句/锁超时、租约/心跳、工具版本/策略及审批绑定后，先启动受控 Worker，
   检查等待、延期、终态及 Artifact，再逐步开放 Writer。不要手工把 UNKNOWN 改成 ready/failed。
8. 记录恢复证据和未解决事项。只有生产部署门禁全部满足并获发布授权后才能切流。

## 只读持久状态核查

```sh
# AGENTFLOW_DATABASE_URL 由安全环境注入，不在命令或报告中显示。
uv run python scripts/audit_durable_runtime.py --since <UTC-Unix-seconds> --limit 1000 --offset 0 --stale-seconds 3600 --timeout-seconds 30
```

使用专用 SELECT-only 角色；PostgreSQL 事务显式 `REPEATABLE READ, READ ONLY`，不加行锁；SQLite 使用
query_only 只读快照，结束恢复连接设置。不调用 Store.claim、heartbeat 或恢复操作。单次扫描最多
10000 Step，流式读取，每条语句限制 5 秒，累计行循环默认 30 秒（可配置 1–120 秒），连接最多 5 秒。
超时/连接失败返回安全错误、退出 1，不能视作健康。字段 `truncated=true` 仅表示部分范围，不是全库证明；
使用报告 `scope.next_offset` 作为下次 `--offset`，保持 since/limit 等过滤条件一致，无需查找 UUID。
offset 必须为 0–1000000 的整数，每页最多 10000 Step，按数据库 StepRow.id 明确排序，ID 不输出。
完整最后一页返回 truncated=false、next_offset=null；如触及 offset 上界仍有数据，则
truncated=true、offset_limit_reached=true、next_offset=null，必须缩小 since 范围，不能视作全库扫描完成。
旧 `--after-step` 过滤器为兼容保留，普通分页不需要它。报告不返回 Run/Step/Session 等原始身份、
工具名、内容或 actor。每个请求使用独立数据库快照，持续插入、删除或更新时分页位置可能移动，出现
重复或遗漏；**多页结果不代表一次完全一致的全库快照**。需要一致全库核验时应在停 Writer 的隔离恢复库执行。

JSON 中 steps 为选定范围的状态计数；waiting 含数量和自最后状态更新以来的最长秒数。
observations 含到期/未来模型重试、过期租约、无推进会话、UNKNOWN 和批准未消耗授权数量。
severe 含未知 Step 状态、revision/快照、Agent/Step 状态、取消后活跃 Step、延期不一致和 Artifact
数量错误的固定码。损坏的状态值只归入 invalid，不把原始数据库值输出到报告。
退出 2 表示严重一致性问题，退出 0 只表示所扫描范围未发现这些问题。
等待审批和对账、过期租约、到期未领取均不自动判失败；按值班 SLA 提醒操作者调查。
工具不逐条重建所有历史转换，完整历史契约由发布测试及 AgentStore 恢复验证覆盖。

Prometheus 是 best-effort 信号，COMMIT 后指标前退出会少计，默认本地 Counter 在进程重启后重置。
共享指标目录不可用、Collector 异常和 /metrics 503 不改变持久状态。审计直接查询数据库事实，不保证
每个 Counter 与历史事件逐项相等。不要把审计或指标作自动补写、计费或第二套事实账本。

## 可选两宿主 staging（当前 NOT VERIFIED）

需要两台独立宿主及一个**专用** PostgreSQL，数据库名必须以 `agentflow_rc_` 开头，例如
`agentflow_rc_staging_<deployment-id>`；仅给测试角色访问该库。部署匹配代码/工具版本，设置 TLS、
connect_timeout、statement_timeout、lock_timeout；用已有 Alembic 初始化到 0005。禁止指向生产库。

协调主机在仓库根目录可使用以下显式夹具（环境 URL 来自受保护 Secret，输出只有合成 Run ID）：

```sh
uv run python -c "import os,sys; sys.path.insert(0,'scripts'); from rc_workload import Workload; w=Workload(os.environ['AGENTFLOW_RC_DATABASE_URL'],'rc-records'); print(w.submit('crash')); w.close()"
```

在两台宿主分别创建本地记录目录，注入 `AGENTFLOW_RC_DATABASE_URL` 与 `AGENTFLOW_RC_RECORDS` 后运行：

```sh
uv run python scripts/verify_durable_soak.py --worker --seconds 600
```

协调主机用相同 Workload 的 `approve(run_id)` 等待并批准具体行动；记录 Action 版本/参数 digest，
批准消耗和 INFLIGHT 出现后终止实际持有租约的 Worker 进程树。另一主机继续轮询，应在租约过期后
持久化 UNKNOWN/WAITING_RECONCILIATION。通过 `reconcile(run_id)` 提交合成外部证据，再验证完成与
Artifact 唯一。使用相同方法提交 retry/unknown/review/normal/cancel 夹具及真实业务取消。
各宿主的合成记录文件用于核对派发；不要依赖本地文件作运行时状态。核对旧 Task LostLease、预算及
幂等键，并对独立 Run 验证隔离。数据库短断、单宿主断链及恢复需由测试环境操作者实施，限制演练窗口，
收集各端只读状态核查和网络证据。最后停止所有进程并按测试资源策略清理专用库；脚本不删除外部库。

以上是可执行的 staging 入口和操作流程，尚未实际运行；单机三进程和临时 PG 重启不是跨主机网络
分区验证。跨主机验收、真实数据库拓扑、连接池容量和长期锁竞争仍是部署门禁。

## 受控真实 Provider（当前 LIVE PROVIDER NOT VERIFIED）

默认 pytest 永远跳过 live，即使开发机已有授权环境变量和 Key。pytest 仅在同时具备
`--run-live-provider`、操作者授权环境变量和专用 API Key 时启用；缺任一条件均不启动付费进程。
普通 push/PR CI 不提供这个选项。离线回归使用模拟 Key 并拦截付费子进程，以 MockProvider 验证显式
开启分支，不发送网络请求。默认 skip reason 为 `paid provider tests require explicit --run-live-provider`。
操作者先给专用 DeepSeek 账户设置金额上限，
核对当时价格并授权**一次**请求，再安全注入 `AGENTFLOW_DEEPSEEK_API_KEY` 与
`AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION=one-paid-request`，显式运行：

```sh
uv run python scripts/verify_live_provider.py --allow-paid-request
```

完成相同授权后，也可显式选择 pytest 入口：

```sh
uv run pytest -q tests/live/test_deepseek_live.py --run-live-provider
```

固定 `https://api.deepseek.com`，默认 deepseek-flash，仅允许通过 `--model deepseek-v4-pro` 受控切换。
使用公开无敏感 Prompt，无工具，单次输出最多 4096 token，
输入小于 1KB，网络 45 秒、步骤 60 秒、总协程 65 秒；无隐藏重试，max_model_attempts=1。
它验证真实 ModelResponse/assistant/stop/可选 metadata 和 Durable Step/Artifact；不依赖自然语言逐字一致。
只报告 metadata 是否存在，不输出正文、usage 值、请求 ID、异常消息或凭据。
UNKNOWN 可能计费，即使最终验收失败；字节/输出 token 上限不保证固定金额。429/5xx/超时仍用确定性
MockTransport，不能安全诱发的故障不向真实 API 注入。

手动工作流 live_deepseek 默认 false，开启表示授权一次付费请求。管理员必须事先为
`live-provider-acceptance` Environment 配置 required reviewers、可信分支限制和专用 Secret，确认平台
支持保护；YAML 的 environment 名称本身不会建立审批策略。不要在不可信分支运行带凭据工作流。
缺少授权/Key 时输出 NOT VERIFIED，显式付费入口返回非零；禁止把此状态记录为实测成功。

## 模型生命周期升级预检（v1.1，2026-10-20）

官方规范名称是 deepseek-flash 和 deepseek-v4-pro。旧 chat/reasoner 已公告于 2026-07-24 停用；
v4-flash 和 v4-flash-vision-exp 的旧模型退役、名称仍由供应商重路由。Core 拒绝全部旧别名，不把重路由当作身份兼容。
来源、接口和 Token 边界见 [请求契约文档](model-request-contract.md)，本轮真实 API 状态为 LIVE PROVIDER NOT VERIFIED。

1. 停止新任务领取及 Writer，盘点所有 Worker/版本。用 SELECT-only 账户从受保护环境提供连接，运行
   `uv run python scripts/audit_model_retry_upgrade.py --limit 1000 --offset 0`，按 next_offset 完成有界核查。
   分页为独立快照，更新中的多页不等于一个全库快照；不要公开原始数据库或私有诊断记录。
2. 记录 legacy_model_sessions、legacy_pending_retry_calls、MODEL_INFLIGHT、unbound_request_sessions、
   terminal_historical_calls、operator_review_sessions。已完成的旧模型档案合法；退出 0 仅说明此页没有处置项，
   2 需要人工审核，1 表示未完成。缺身份不能依据当前模型设置或响应 metadata 回填。
3. 等待可确认的在途请求提交；失联请求按原租约/UNKNOWN 恢复规则处理。停止 Worker 不证明外部请求未发生。
   工具 UNKNOWN 仍需人工对账，模型 UNKNOWN 不重发；不改 FAILED/UNKNOWN、fencing token、Contract 或 Permit。
4. 停止 Writer 后完成加密、受限权限的数据库备份并核对恢复方案。v1.1 不新增 schema，保持现有 0008；
   从较老版本升级仍使用正常迁移，不编辑 0001–0008。逻辑备份演练不证明零数据丢失或 HA。
5. 部署一致的模型配置。新默认只适用于新身份；已绑定 Session/失败调用不换模型。继续配置旧别名会在请求前
   ModelDeprecated；改成新模型重试旧调用会 ModelRequestContractChanged。规范模型原 v1 请求不变时可合法重试。
   Session 后续轮次仍受 profile 约束，不会因新默认重新绑定。不要反复调度这些拒绝来消耗普通 Step 重试。
6. 必须重做旧任务时，由操作者先评估已完成工具、副作用、审批和 UNKNOWN，再创建明确的新执行；不自动复制或重放。
   取消过的 Workflow 保持取消。重新运行只读审计，按原最小权限、审批绑定、租约和数据库时钟条件恢复 Worker。

strict_token_window 默认关闭；没有经验证的完整请求 Counter 时开启会安全拒绝，而不是按字节估算放行。
本轮只有 Mock/MockTransport 的接口合规证据；真实 Provider、跨宿主和目标生产部署仍须单独授权验收。

## Evidence Disclosure v3 升级与恢复

1. 停止领取及全部 Writer/Worker，备份后统一升级，避免旧二进制继续执行按名称授权的 v2 公开摘录。
   本次无新迁移，保持 0008，不编辑历史迁移。以受保护 SELECT-only 连接运行升级审计的全部有界页。
2. v1/v2/v3_checkpoint_sessions 是最后一次尝试的版本计数（含终态）；v2_public_excerpt_policy_sessions
   来自持久 StepSpec，也计零尝试会话。只对活跃、非终态会话计算未绑定公开规则/升级阻断/人工处置，
   不因历史 v2 存在就重新启动终态 Run。合法普通等待与升级错误分开查看。
3. v1 新预约 AgentEvidenceV1ReadOnly；v2 公开新预约 AgentEvidenceV2PublicReadOnly。
   不编辑旧 Manifest、RequestContract、RunSpec、Binding、预算或 token 来绕过。
   v2 metadata_only 可按原请求身份继续，UNKNOWN 不重发。失联模型及工具仍按原租约和人工对账规则处理。
4. 需要公开摘录的新 v3 Workflow 必须在可信注册配置中逐项批准完整 ToolBinding/路径；
   工具版本、策略摘要、Schema、description、timeout、副作用或审批策略改变后，旧规则不授权新身份。
   数据库中旧 Binding 不能由当前注册表代填；公开分类不是自动 Secret/PII 检测。
   invoke 实现/外部服务语义改变而元数据不变无法自动识别，部署者必须更新版本。
5. 如确需新执行，先审查旧行动、副作用、已消费审批和 UNKNOWN，再明确建立新 v3 Run；不自动复制重放。
   已存在的合法历史版本可被显式授权，当前部署升级不应错误重解释旧输出。提取、转述和执行权限不同。
6. 在隔离环境执行 verify_runtime_backup.py，核对 v1/v2/v3 JSON、绑定/幂等键/用量/审批/对账和 Artifact；
   恢复端不得重派已完成行动或 UNKNOWN。新元数据受 8 MB 配额及控制空间预留，不删记录腾空间。

出处真实性不证明现实事实；摘录仍可能丢失语义、误导模型。真实 Provider 和跨宿主演练仍为 NOT VERIFIED。
回滚旧二进制前先停止 Writer 并处置新会话，旧 reader 不能理解 v3；不要把 schema 未变误作二进制兼容。
