# Production RC Staging & Evidence Gates v1

新的操作者采集流程见 [RC Host Witness v1](rc-host-witness.md)：identity/preflight/status
与实际执行分离；probe/run 仍需环境 UUID、明确授权、TLS/OID/0008、干净部署、槽位锁及
专用合成批次。默认不连接外部环境。run 只监督自己启动的本机固定子进程，不主动注入远端故障。
日志不包含输入、Prompt、参数、结果或真实 token；缺失、截断、时钟不明和提交确认丢失不会
升级成成功。G 的同调用 429/延期使用 retry fixture 的 case G 采集模式，既有单尝试耗尽 fixture
继续用于终态/备份测试。没有独立签名和 Reviewer 时，只能得到 NOT_VERIFIED unsigned draft。

本轮只验证离线软件门禁。**真实双宿主、真实 Provider、真实 Token Conformance 均为 NOT_VERIFIED**。
软件 CI、单机三进程、逻辑备份通过，不等于目标生产环境已放行。没有自动远程故障操作。

本轮两宿主交接使用下方 [操作者准备包](#两物理宿主操作者准备包)：逐 Case 的十二项记录、
独立材料清单、时间预算及 STOP/HOLD/RECOVERY。它是待人工填写和审批的手册，不是授权文件。

## 证据与信任边界

`scripts/rc_staging.py` 定义冻结的 `RCStagingEvidenceReport(schema_version=1)`。
报告绑定代码 SHA、应用版本、实际数据库 revision、环境 UUID、授权状态及 UTC 起止时间。
每个 Gate 只能为 PASS / FAIL / NOT_VERIFIED / NOT_APPLICABLE；PASS 必须含检查来源、
非敏感检查结果和规范化 UTF-8 JSON SHA-256。预检 PASS 必须完整覆盖八项检查。
只有 CLI 本次实际查询成功后才生成预检 PASS，不读取操作者提交的 `passed=true` 或既有 Gate 报告。
证据摘要不是签名、不是身份认证，也不是加密；类型校验本身不能证明文件来源。保存输出时绑定
受保护的执行日志、版本、操作者审批记录及独立审查来源，不能信任任意上传的报告。

v1 自动收集器**不认证**物理宿主独立性、网络故障、真实 Provider、备份恢复、fencing、Admission、Summary 或真实
Token 证明，拒绝将这些字段构造成 PASS；它们保持 NOT_VERIFIED，实际执行记录由发布审核单独验收。
这是明确的保守边界：没有独立证明验证器，不接受一组任意文件的 digest 作为跨宿主通过证据。
宿主机器标识只输出 SHA-256，克隆机器标识、容器或伪造的清单都不能证明物理独立性。
真实 Provider 的单请求报告独立于 staging 预检，详见下节；不能用 Mock 报告填充该 Gate。
v1 不导入外部报告来提升这些 Gate；以后接入独立证明验证器需显式升级证据契约。

报告不包含连接 URL、密码、原始身份/Token、Provider request_id、Prompt、工具结果或用户输入。
私有 plan 只保存环境/批次 UUID、本次合成 Run ID/scenario，Worker JSONL 还含真实 fencing Token：两者仅放私有目录，
不上传公共 CI Artifact。JSONL 只是派发诊断，数据库才是权威状态；不能靠合并两个 JSONL 宣称恢复成功。

## 独立只读预检

1. 操作者另行批准两个独立宿主、专用 PostgreSQL、固定合成数据、维护窗口和数据库故障范围。
   此任务没有获得该授权，不执行以下外部操作。预检账户仅 SELECT；执行账户另行授权。
2. DBA 独立初始化/审批迁移到当前实际 Alembic Head（目前 `0008`）。所有工具只读检查 revision，
   **不自动迁移外部数据库**，不编辑 0001–0008。先停止旧 Writer；不可将升级混入故障演练。
3. DBA 核验 TLS 证书对应的专用服务器/数据库，分配新环境 UUID，记录数据库 OID；在该专用库设置
   数据库 comment 为 `agentflow-rc:<环境 UUID>`。这是管理员登记，不由脚本自动创建。
   TLS verify-full、独立 OID、管理员 marker 和环境审批共同核对；单独库名前缀不足以证明隔离。
   配置生产标志或与 `AGENTFLOW_PRODUCTION_DATABASE_URL` 同一目标时拒绝。
   工具无法发现恶意管理员把生产环境重新标成 staging；部署审批和凭据隔离仍是必要前提。
4. DB 角色不能为 superuser/createdb/createrole/replication/bypassrls，不能拥有数据库、表或 schema
   CREATE 权限（含继承权限）。预检角色只 SELECT 全部应用表和 alembic_version；执行角色只需
   SELECT/INSERT/UPDATE（可按既有 Store 需要给 DELETE），禁止 TRUNCATE/REFERENCES/TRIGGER。
   alembic_version 始终只 SELECT。设置角色默认 statement_timeout ≤ 5000 ms、lock_timeout ≤ 2000 ms，
   且均非零。预检在 READ ONLY 中使用最多五次有界查询，局部 5s/2s 超时；不扫描业务正文。
5. 使用同一干净 Git checkout，在 API/两个 Worker 实际部署环境各运行：

   ```bash
   uv run python scripts/verify_rc_staging.py identity --role api
   uv run python scripts/verify_rc_staging.py identity --role worker
   ```

   将收据通过受保护通道交给操作者，不能用手填 SHA 替代实际部署核对。`DeploymentIdentity` 包含
   build SHA、应用版本、Core 源码摘要、RC 工具实现摘要、Admission/Observed Policy 摘要、
   UsageMapping 摘要、支持的 Request Contract `(1,2)` 和真实可用 Counter Profile 摘要（当前为空）。
   合成 token_profile 测试 Counter 不属于生产 Profile。API/Worker 清单必须完全一致，
   两 Worker 机器标识不能相同；仍需独立资产清单证明它们来自两台真实宿主。
   本工具的身份只覆盖固定 RC fixture 部署，不是任意 API 插件/自定义工具的远程证明。
   镜像/安装包不含干净 Git checkout 时此入口拒绝生成身份；需另行审核构建来源和镜像摘要，
   不能在配置中手填 SHA 绕过，也不能拿宿主上的另一份 checkout 代替运行中的 API 身份。
6. 在仓库之外的私有文件写入以下结构，`hosts` 按 API、Worker A、Worker B 排列，嵌入实际收据：

   ```json
   {
     "environment_id": "<新环境 UUID>",
     "database_oid": 12345,
     "expected_identity": "<替换为实际 DeploymentIdentity 对象>",
     "hosts": ["<实际 API HostIdentity>", "<实际 A HostIdentity>", "<实际 B HostIdentity>"],
     "max_workers": 2
   }
   ```

   尖括号为待替换结构，不是有效配置。配置 ≤ 32 KB。配置、收据、plan、records 均放在仓库之外的
   私有目录，否则干净构建检查会拒绝后续启动。只通过环境变量提供数据库 URL，使用
   `postgresql+psycopg`、`sslmode=verify-full`、`connect_timeout` 1–5 秒；不打印 URL。
   显式指定 `AGENTFLOW_ENVIRONMENT=staging`，设置 RC Admission/Observed Policy 环境变量与部署一致。
   通过已存在的 `AGENTFLOW_RC_DATABASE_URL` 传入 SELECT-only 连接后执行：

   ```bash
   uv run python scripts/verify_rc_staging.py preflight --read-staging --config <私有配置> --environment-id <UUID>
   ```

   无读授权开关不连接数据库；配置、TLS、角色、Schema 或构建不匹配返回非零并阻止 Worker。
   即使成功，也只证明**这次预检的范围**，不证明物理独立性或 A–J 已执行。

## 明确授权后的合成执行

执行账户只访问专用测试库。需同时提供 `--allow-staging-execution` 与环境变量
`AGENTFLOW_RC_STAGING_AUTHORIZATION=synthetic-staging-only:<UUID>`，并匹配配置环境。
不提供这些条件时，工具在连接前拒绝。只读账户不能用于执行；不使用生产 URL/凭据。

```bash
# 首次 seed 为 plan 建立新的私有父目录，必须事先不存在；每 plan 最多 20 个合成 Run。
uv run python scripts/verify_rc_staging.py seed --config <配置> --environment-id <UUID> --allow-staging-execution --scenario review --runs 1 --seconds 60 --plan <新私有目录/plan.json>
# 两台宿主分别 slot 0 / 1，各自独立 Engine/Pool，最多两个活动槽位。
uv run python scripts/verify_durable_soak.py --worker --config <配置> --environment-id <UUID> --allow-staging-execution --slot 0 --seconds 600
# 操作者只针对上述私有 plan 中的合成行动批准；对账也须有人工确认合成执行结果。
uv run python scripts/verify_rc_staging.py approve --config <配置> --environment-id <UUID> --allow-staging-execution --plan <plan.json> --seconds 60
```

`approve` 可替换为 `reconcile` 或 `cancel`，分别调用既有 CAS/审批绑定/对账/取消事务。
`reconcile` 只适用于固定 `{value:1}` 合成工具，不是业务对账服务，不允许对任意业务行动声明成功。
seed 场景包括 final、normal、review、crash、unknown、retry、cancel、model_rate_limited、summary、
summary_consumption，以及包含上述十个场景的 acceptance_suite。单场景 plan 或 suite 总 Run 数均
不得超过 20，且不得超过独立审批上限；
重复创建 plan 不是额外授权。操作时核对持久化批次标记，不允许拿另一批次的 plan 处理当前 Run。
中途提交确认丢失时停止，先只读审计，不盲目重新 seed。
Worker 执行循环最长 3600 秒；合成执行阶段 deadline 最长 600 秒（默认 60），另有预检和有界数据库/
清理耗时。Worker 正常退出只表示本轮循环结束，不代表所有 Run 成功或发布门禁通过。
槽位使用独立 PG session advisory lock，不替代 Step lease/fencing；监测连接丢失时停止本轮 Worker。
只允许直连 PostgreSQL 或经独立核对的 session pooling；transaction pooling 不支持此 session 锁。
v1 不自动发现中间代理，路由/池模式未知时不得演练；网络黑洞下还须由 DBA 验证 TCP 存活超时。
不会启动远端进程、改防火墙或自动删库；原 Runner 最多五秒取消清理不变，不能强杀已进入驱动的线程。

## A–J 验收记录和停止条件

历史协议边界见 [RC Independent Acceptance](rc-independent-acceptance.md)：
v1 仍拒绝全部非预检 PASS；v2/v3 的 verify 和 Strict v1 precheck/verify 只作历史核查。
v3 issue/snapshot 继续用于当前准备和采集，正式最终入口只能用 custody-precheck/custody-verify。
离线接纳保存证据不等于实时数据库核验或生产发布授权；缺少独立宿主/故障证据保持 NOT_VERIFIED。
`seed --scenario acceptance_suite --runs 1` 可在原有授权下生成十个场景的同批次计划，
`--plan-index` 选择一项审批/对账/取消，数量、私有目录及批次身份限制继续生效。

每项记录受保护的开始/结束时间、两宿主部署收据/独立资产来源、故障授权、合成 plan、实际数据库
审计前后页、断言来源和审查人；公开报告只含状态/摘要。故障由操作者控制，只终止本次启动的 PID。

| 场景 | 操作者步骤与数据库检查来源 |
| --- | --- |
| A | 两宿主启动后 seed normal；核对真实领取 token/lease 及每次 Attempt/Action 唯一性；不是只比较进程日志 |
| B | seed crash、批准；确认持久 INFLIGHT 后终止 A 的本轮进程；B 在租约过期后只能 UNKNOWN + waiting；不得派发第二次 |
| C | 单独批准 A 到数据库的网络断连（工具不代做）；B 合法接管后，用私有旧 token 检查真实 LostLease；复用 rc_workload.Workload.fenced_claims，私有记录只供核对 |
| D | review 的 Action 已完成且 Approval 已消费，但 Step 仍有合法恢复读取边界；两宿主保留真实 resume，核对 consumed_at/Action/幂等键不变，无重复 dispatch；终态 Step 不能重新 claim，缺边界则 HOLD |
| E | unknown 批准并挂起；重启 B 不派发；操作者核实合成结果后显式 reconcile，再恢复 |
| F | 在相同 ProviderAdmissionPolicy 下两宿主争抢；核对 Permit 唯一键/配额及重启后有效计数；不使用本地 Counter 判断 |
| G | retry（suite 索引 5）+ 已启用共享 Admission + case G Witness；首次 Mock 429/defer 后取 before，同一采集进程继续观察未到期 claim，再记录同 ModelCall 的合法下一 Attempt；model_rate_limited 是单尝试耗尽，不能替代 |
| H | summary / summary_consumption 在不同持久边界终止本轮 Worker；另一宿主校验来源、Consumer View/Contract 身份与 Artifact 唯一性 |
| I | review 批准后、重新 claim 前 cancel；以及模型/工具运行中 cancel；迟到旧提交必须拒绝，不发布第二 Artifact |
| J | 独立批准专用 DB 连接中断/恢复，保留安全停止结果；审计区分过期 lease、合法 waiting、UNKNOWN、delayed 和终态，不把 unavailable 当作空库/成功 |

复用既有 `tests/runtime_assertions.py` 的真实持久状态检查、`audit_durable_runtime.py` 和
`audit_model_retry_upgrade.py`。审计命令使用 SELECT-only URL、limit ≤ 10000、offset、超时；读取全部
有界页，不输出原始身份。不同请求不是同一个数据库快照；活跃写入会导致分页移动，审查时记录 Writer
是否停止。Audit 的退出码 0 只代表本页无已知严重问题，**不证明所有 A–J 不变量**。
跨宿主收据/私有 fencing 记录由独立操作者在受控目录汇集；不要将本地 JSONL 当成权威历史。

停止条件：旧 token 提交成功、完成/UNKNOWN 重派、撤销授权被消费、未知 usage 被计零、重复 Permit/
Summary/Artifact、取消复活、超时/失去数据库连接、严重 audit finding 或状态无法核实。停止创建新 Run，
保留数据库事实，手动停止本轮 Worker；不自动修补/删库/修改状态。必要 UNKNOWN 只按既有人工对账处理。

## Provider 与 Token 独立门禁

真实 Smoke 沿用 `verify_live_provider.py --allow-paid-request`，同时需要专用 Key 和
`AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION=one-paid-request`。默认 pytest 还必须显式
`--run-live-provider` 才能运行该测试；普通 CI 永不默认调用，手动 Workflow 默认关闭。
固定公开 Prompt、最多一次请求、45 秒 HTTP 超时、4096 输出上限、max_model_attempts=1；
429/未知结果不发第二请求。账户侧费用上限须另设，UNKNOWN 可能已计费。
报告分别记录接受请求、合法 assistant、可选 usage/request_id 是否存在（不含值）、模型名是否匹配
冻结身份、Attempt 确认、Step/Artifact 提交。模型名缺失为 NOT_VERIFIED，不能推断相等。
MockProvider/MockTransport 仅证明这个入口的软件行为，没有真实请求则 LIVE PROVIDER NOT VERIFIED。

Counter UNKNOWN 不可开启 strict；EXACT 必须审核托管模型修订/完整 framing 等价性；UPPER_BOUND
必须有覆盖声明全部请求范围的上界证明。离线向量或一次 Usage 匹配都不充分。
目前没有真实 DeepSeek Profile，strict 继续 fail-closed；LIVE TOKEN CONFORMANCE NOT VERIFIED。
不修改历史请求摘要/Usage，不以报告中的 model_present 或一次对照宣称普遍精确计数。

数据库逻辑 pg_dump/pg_restore 验证与跨宿主网络恢复、HA/PITR 是三项不同门禁；本工具不认证后两项。
最终生产放行还需独立宿主证据、实际 A–J 审核、目标环境备份/恢复与 HA 策略、真实 Provider 验收；
启用真实 strict 前还需独立计数证明。无法执行时保留 NOT_VERIFIED，不影响离线软件门禁真实通过记录。

## 正式验收必须采用 Strict Release Evidence Gate

使用 [Strict Release Evidence Gate v1](rc-independent-acceptance.md#strict-release-evidence-gate-v1)
的 `verify_rc_release.py custody-precheck/custody-verify` 入口。`verify_rc_causal.py` 保留历史 v3 行为；其 PASS
不能替代 Journal Provenance、精确 Signed Host 对应和 Reviewer 的完整集合审核。
v2 的 A/F 已复现不同 Step、不同预约可组合为 PASS，历史报告仅保留历史含义，不作为新的因果验收。
每个 Witness 固定 before/after、Challenge/batch、故障窗口、真实目标和宿主；F 必须是同配额池，
审批/对账/摘要都必须有真实关系。缺失为 NOT_VERIFIED，可信矛盾为 FAIL。两宿主、实际故障、
专用 staging 连接及 Provider 付费请求仍需操作者分别授权；本轮没有自动执行这些操作。
签名与独立 Reviewer 不能替代资产核验，exit 0 只接受保存证据，不是生产放行。

采集与故障执行需要本章的独立授权，离线审核不授予操作权限。操作者依次完成：

1. 从干净部署取得完整 identity（runtime digest 包含严格入口），冻结 plan 与有效 v1 预检，
   通过原 v3 issue 注册新 Challenge；独立提供 trust 文件 SHA 和 collector SHA。
2. 在获准环境收集 A–J before/after/fault 和配置中全部 worker 的完整 sealed journal。
   受保护签名器核对现场来源、完整 Binding/process headers，并签署原 v3 收据。
3. 按手册固定 bundle 布局，在新的私有 custody store 做 Prepared Freeze，导出 UNSIGNED 待签请求。
   独立签名方核对原 v3 精确 bytes、来源与固定 trust。收齐非 Reviewer 返回后可做第二份 Prepared，
   Reviewer 只签署确切 reviewed_evidence_digest 与冻结 envelope；源或签名漂移拒绝交接。
   同路径 Signed Receipt/Request 冲突一律拒绝；已有签名不重新导出为 UNSIGNED，也不改原始字节。
4. Final Freeze 固定全部签名、Reviewer 和输入；先 custody-precheck，最后一次 custody-verify。
   记录 Manifest SHA、完整字节摘要、原 registry 与受保护 store；不把原路径存在当作不可变性。
   退出码、各分层状态和消费结果共同记录。已登记最终尝试
   不重用 Challenge，失败补证先申请新授权；不删除、复制消费记录，不运行生产迁移。

冻结使用私有未发布工作对象，完整重验后不覆盖地原子发布；同内容重试也必须核验全部原始
bytes/Manifest。中断工作对象保留，旧 SHA 半成品按手册显式人工隔离，不清除 registry/spent。
Windows 的进程退出测试不能证明目录掉电持久性；不支持不覆盖发布原语的平台直接拒绝。

software_validation 仅为离线验证器输入/检查，自动化测试成绩单列。保存证据通过、真实宿主与
故障通过、正式生产批准分别记录。SQL 锁竞争、物理故障域、cross_host_recovery、真实 Provider、
Token 与 production_release 不由离线工具晋升 PASS。registry 仍是单机文件消费，不是分布式服务。

准备材料可由 `scripts/rc_operator_readiness.py` 离线核对，GO_FOR_OPERATOR_REVIEW 仅表示材料
与固定范围一致，execution_authorized=false；真实环境范围、两 Host 部署清单、OID/Marker/TLS/0008、
任务上限、故障窗口、独立审批人、停止条件、UNKNOWN/Lease 恢复、保留策略和 A–J 手册原件仍须
操作者独立核实并另行批准。未获批准不连接 Staging、不执行故障，也不接入生产签名私钥。

## 两物理宿主操作者准备包

此章节是 DBA、独立资产审核、Build/Infrastructure/Fault/Host/DB Signer、Reviewer 与操作者的
共同填写底稿。没有提供真实环境或独立审批原件时，全部 Case 的现场准备为 NOT_READY、
现场结果为 NOT_VERIFIED。空白、占位符和软件测试成绩不能当成已批准事实；不自动生成审批文件。

### 独立材料和职责清单

每行由责任方填写原件位置、版本/公开摘要、核对人、核对时间和待办；敏感原件留在仓库外。
OperatorMaterials 的 JSON 仅使用下表列出的既有字段，extra=forbid；其余现场事实放审批附件，
不要向 JSON 塞入 passed=true、物理认证 bool、密码、PID 明文或新的协议字段。

| OperatorMaterials 字段 | 独立原件、责任方与核对内容 |
| --- | --- |
| schema_version | 固定 1；这是材料结构版本，不是授权状态 |
| environment_id / scope_approval_digest | 独立批准的专用环境、演练目的/范围、允许的 Run 总量及只读/执行权限；审批方核对原件 |
| operator_identity / independent_approver_identity | 两个不同责任主体的原件及职责；摘要不同不等于已认证身份 |
| host_inventory_digest / worker_hosts | 独立资产方核对 A/B 两物理宿主、故障域、现场身份、虚拟化/克隆关系；HostIdentity 摘要仅作引用 |
| deployment_sha | Build 方核对 API/A/B 同一干净 checkout、实际运行代码、构建链、制品和镜像摘要；不能拿旁边 checkout 替代镜像内身份 |
| trust_sha256 / collector_sha256 | 独立分发的固定 Trust Root 和采集器 bytes；角色、Host、公钥有效期/轮换审批完整，不能从上传包注册 key |
| private_plan_sha256 / preflight_sha256 | 精确 plan 与本次 v1 只读预检原始 bytes；批次/Run/Step 映射和当前部署一致，预检在 issue 前不超过一小时 |
| database_oid / database_marker / alembic_revision | DBA 核对专用数据库 OID、agentflow-rc:UUID、0008，确认没有生产业务 Writer；迁移由 DBA 另行审批，工具不代做 |
| tls_preflight_digest | DBA 原件覆盖 verify-full 证书/端点、直连或 session pooling、最小权限、非零 statement/lock/TCP 超时；读写账号分离 |
| synthetic_task_limit | 审批总上限 1–20，plan 的实际 Run 数不得超限；多 plan/重复 seed 不增加额度 |
| fault_window_start / fault_window_end | 审批的实际 UTC 故障区间，严格递增、跨度不超过 600 秒，完整位于本次 Challenge 内；每个 Case 的区间不得越界 |
| stop_conditions_digest | 每项 STOP/HOLD、停止新 Run/Writer 的责任人、允许处理的 PID/连接白名单及撤销方式 |
| unknown_recovery_runbook_digest / lease_recovery_runbook_digest | UNKNOWN 原结果核实/显式对账，租约到期/接管/旧 Owner 恢复和遗留进程清点；不能自动重发 |
| private_retention_policy_digest | 保留期限、权限、审计访问、备份保管人及归档批准；保留 registry/spent/Prepared 链和未完成对象 |
| aj_runbook_digest | 本章实际填写后的 A–J 运行单，含对象映射、时间预算、角色到场与签署周转承诺 |

审批附件必须另列真实物理资产证明、构建/镜像来源、每个 Case 的允许 PID/进程树/连接与
故障/恢复动作、DB 账号权限原件、独立签署职责和响应时间、明确的授权签字。自填摘要、冻结
bytes、机构认证、真实故障域和正式授权是五类不同事实，不能互相替代。不得发送私钥或连接 URL。

审批须分别界定只读核查、有限 Seed/准备操作及各 Case 的故障执行。Readiness 依赖已存在的
plan/Challenge/冻结材料，不能反过来授权 Seed：必须先有明确的有限准备授权才能建立真实
合成 plan；缺准备授权时仅交付文档与缺件表，不能为了凑齐材料先执行。准备授权不包含故障许可。

| 责任方 | 必须独立核实和签署的范围 |
| --- | --- |
| Build Signer | 共享 build.json：实际部署/制品来源与同一冻结 SHA |
| Infrastructure Signer / 资产审核 | 共享 infrastructure.json 与私有资产原件：两物理宿主和故障域；软件 Host ID 不足以认证 |
| DB Signer / DBA | 每 Case 的 before/after PostgreSQL 只读快照、OID/Marker/批次/时间和对象关系 |
| Fault Signer | 获准的实际动作、执行/恢复时间、PID/nonce/连接范围；binding 中完整日志和 process_headers，与 fault_record_digest 对照 |
| Host A / Host B Signer | 各自受保护进程的完整 journal、准确派生 payload/envelope、同一 Binding、Challenge、部署、Host 身份 |
| Reviewer | 最后审核每 Case 全部非 Reviewer 收据及共享 Build/Infrastructure 的确切 evidence_set_digest，另核原件与因果关系 |
| 操作者 / 独立审批人 | 执行与批准职责分离；Reviewer 不替代故障执行、DBA、资产证明或正式发布批准 |

Key 仅在已批准的外部签名设施中使用；本仓库不生成或调用生产私钥。是否允许某机构兼任某
职责须独立审批，不能因工具接受某个公钥就认定其组织独立性。缺角色、原件或有效公钥即 HOLD。

### 时间窗口与可行性裁决

源码约束：AcceptanceChallenge 最长 3600 秒，v3 issue 固定签发一小时；OperatorMaterials
故障窗口不超过 600 秒且落在 Challenge 内；单次 HostCollector 1–600 秒、200 事件/1 MB。
每次签名校验、冻结和最终消费仍检查当前有效期。预检 PASS 不预约有效期或一次性消费权。

操作者在 issue 前填写实测预算：各 Case 前置准备/前后快照/协调/故障/恢复耗时、角色签署及
转交 SLA、Reviewer 全集合审核、Prepared/Final/双重校验耗时、可核实的时钟误差与退出余量。
计算故障跨度 `max(故障结束)-min(故障开始) <= 600s`，并计算从 issue 到最终 verify 完成的
最长依赖路径 `采集 + 转交/签署 + Host 返回 + Reviewer + Final/验证 + 余量 <= 3600s`。
可并行的签署也要计入最慢角色的依赖路径，不能简单相加或假设立即响应。

本轮无真实两宿主延迟、现场动作耗时和签署 SLA，窗口可行性为 **HOLD / NOT_VERIFIED**。
不能按十项各 600 秒串行安排：仅采集上界就可能超过一小时；长时间空轮询还可能先达到
journal 事件上限。应先完成独立批准的无故障排练、核对真实里程碑和签署周转，再决定是否 issue。
本机 pytest/Soak 耗时不是现场 A–J 时间预算。故障窗口也不是每项各自获得 600 秒额度。

若预计超时、证据时间越界或签名不能及时返回，停止该次尝试并保留原件，申请新的演练授权、
新 plan/Challenge 和独立完整证据集合；不能延长 TTL、改时钟/时间戳、复用旧 Challenge、改写
旧签名或把多个 Challenge 的 A–J 拼成同一 Final。未消费但已过期也不能继续最终验证。

### Case 共用的记录和判定规则

每个 Case 填写以下十二行，记录真实对象摘要与私有映射；下列场景名只是选择提示，不能当作
对象身份。suite 索引从零开始：final=0、normal=1、review=2、crash=3、unknown=4、retry=5、
cancel=6、model_rate_limited=7、summary=8、summary_consumption=9；应复核实际 plan，不能硬套索引。

**排程限制：** `--plan-index` 只选择 approve/reconcile/cancel 的一项，不给 Worker 增加领取
过滤。Core Store.claim 仍全局领取；专库、获准批次、其他 Writer 静止与每阶段 ready 集合须由
操作者核对。acceptance_suite 不是自动按 A–J 顺序执行的 Campaign。不得为了制造指定窗口
直接改业务行、重置终态 Step 或创建第二调度器；不具备合法目标/恢复边界则 HOLD。

每项快照使用同 Challenge/环境/批次的 v3 SELECT-only 采集，包含该批次真实对象及必要的全局
ProviderPool；记录数据库时间与 Collector header。before/after 之间必须包围该 Case 签署的
故障区间。原子快照与独立分页 audit 分开，后者不能拼成同一时点。所有 Case 的签署来源均为
上表的 DB before/after、Fault、Host A/B、共享 Build/Infrastructure 和最终 Reviewer，不能省略。

以下 PASS 仅描述保存证据契约满足；物理认证、SQL 锁竞争及正式授权另列。可信矛盾/签名错误
为 FAIL；来源缺失、时钟不确定、缺真实动作/边界为 NOT_VERIFIED，并 HOLD 本次 campaign。
实际执行状态当前全部未填写，不能根据这张运行单预先标 PASS。

#### A：同 Run / Step / Lease 的并发领取

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | 目标 ready、无有效 lease，确认其他可领取对象和 Writer 不会干扰，before 保存该 Step |
| 2 私有对象 | normal 的实际 Run/Step；记录唯一 lease 摘要及 Action/Attempt 身份，不用另一 Step 代替 |
| 3 A/B 角色 | A/B 各独立连接、slot 0/1，协调真实 probe；角色以实际唯一赢家为准 |
| 4 操作者步骤 | DB before 后同步发起两次 probe，立即核对真实返回、有效租约与完整 invocation 区间，再取 after |
| 5 故障与恢复 | 获准的竞争触发/释放由操作者记录；无实际触发不签 Fault，无故障时不凭空补故障声明 |
| 6 PG 前后快照 | 同 ready→running Step/Run，唯一授予的 lease、真实 DB 时间和引用闭合 |
| 7 Host Witness | 两完整 journal；一次 accepted、另一次有有效租约依据的 rejected，扣除误差后调用必然重叠 |
| 8 签署来源 | 共用签署表全部角色；资产原件与调用观察分开，Fault 绑定两个进程 header |
| 9 Reviewer 因果 | 同 Step/lease/批次，顺序领取或 unavailable 不是并发拒绝；调用重叠不证明 SQL 锁竞争 |
| 10 判定 | 唯一 Owner 且严格重叠可满足保存证据；双 Owner/引用冲突 FAIL；顺序/不确定/缺租约为 NOT_VERIFIED，transaction_contention 仍 NOT_VERIFIED |
| 11 STOP | 重复授予、非目标领取、数据库错误、时钟误差跨窗或无法排除顺序 |
| 12 人工恢复 | 停新领取并清点本轮进程；保留可能已授的 lease，按原到期/接管程序处理，不直接清除 token |

#### B：中断后 UNKNOWN，不重复派发

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | crash Run 已批准且真实 Action INFLIGHT、lease 有效；仅 waiting_approval 不够 |
| 2 私有对象 | 同一 Run/Step/Action/Approval/幂等键和租约摘要 |
| 3 A/B 角色 | A 为获准父监督器及其本轮 child；B 为租约到期后的恢复者 |
| 4 操作者步骤 | 确认实际 dispatch 和 before，核对本轮 PID/nonce 后人工终止允许的 child；B 到期后真实恢复，取 after |
| 5 故障与恢复 | 独立 Fault 记录中断、退出发生在原 lease 到期前；B 不重复调用，保留 UNKNOWN 等人工对账 |
| 6 PG 前后快照 | 同 Action inflight→unknown、Agent waiting_reconciliation、派发/Attempt 身份不重复 |
| 7 Host Witness | A 父进程实际观察退出与子进程前缀；B 的实际恢复读取；被杀 child 无 seal，不能补封 |
| 8 签署来源 | 共用角色；签署完整的父 journal 与 B journal，未封 child 仅留私有事故附件，不能塞进严格 bundle |
| 9 Reviewer 因果 | 退出对应 before 的真实 lease/INFLIGHT，UNKNOWN 来自同 Action 的恢复，不是另一个 Run |
| 10 判定 | UNKNOWN 且零重派可满足；重复 dispatch FAIL；缺退出前存活/时序/完整 journal 为 NOT_VERIFIED |
| 11 STOP | 退出晚于 lease、PID 不在批准清单、进程来源不明、UNKNOWN 自动重发 |
| 12 人工恢复 | 冻结 Writer、保存未知结果及子进程现场；只核实后显式 Reconciliation，不把 UNKNOWN 改为成功或失败 |

#### C：新 Owner 接管后拒绝旧 token 写入

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | 目标 running、旧 Owner/lease 有效；待接管对象和批次明确 |
| 2 私有对象 | 同一 Run/Step，旧/新 lease 摘要；原调用需要的 token 只留私有进程内存 |
| 3 A/B 角色 | A 必须保留能恢复并真正晚到提交的原进程；B 是实际新 Owner |
| 4 操作者步骤 | before 后按单独审批暂停 A 或中断其专用连接，等 lease 到期，B 真正接管；恢复 A 的原调用并观察写拒绝 |
| 5 故障与恢复 | 记录允许的 PID/连接、开始/恢复时间；没有批准的暂停/恢复办法就 HOLD，不自动断网 |
| 6 PG 前后快照 | 旧/新 lease 不同，新授予已提交；旧写不改变新 Owner 的持久状态 |
| 7 Host Witness | B 的 accepted claim 和 A 的真实 stale_commit/LostLease，不能用 load 异常或死进程 JSON 代替写 |
| 8 签署来源 | 共用角色，Fault 核对两个进程 header 和真实连接事件 |
| 9 Reviewer 因果 | 新授予早于旧写拒绝，拒绝绑定原 token、同 Step；不借不同 Run 的 LostLease |
| 10 判定 | 实际旧写拒绝可满足；旧写成功 FAIL；只有新 Owner 或缺活的旧调用为 NOT_VERIFIED |
| 11 STOP | 旧写成功、连接范围越权、无法证明新 Owner、旧进程身份未知 |
| 12 人工恢复 | 停旧 Writer、按新 Owner 的数据库事实接续，清点遗留进程/lease；不复活旧 token 或删除历史 |

#### D：已消费 Approval 不重复执行

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | review 的 Approval 已消费、Action 已终态，且 Step 仍有合法恢复读取窗口 |
| 2 私有对象 | 同 Action、Approval、binding/idempotency digest、consumed_at 与 Step |
| 3 A/B 角色 | A 在获准边界取得原动作结果；B 在租约交接后读取该终态 Action |
| 4 操作者步骤 | 仅对 plan 的指定 review 批准一次；捕获上述 before 后按获准方案交接，观察真实 resume 与 after |
| 5 故障与恢复 | 审批方案须明确 Action 已完成而 Step 尚未终态的中断点；没有这个边界则 HOLD，不强行再批准 |
| 6 PG 前后快照 | 同 Approval/Action 记录不变、consumed_at 不变，无新派发或工具结果 |
| 7 Host Witness | 实际恢复 load 的 resume，附 approval_ref/action_ref；全部 journal 中无同 Action 新 dispatch |
| 8 签署来源 | 共用角色；审批原件与 Runtime Action Approval 分别核对 |
| 9 Reviewer 因果 | consumed 授权和终态 Action 前后完全相同；终态 Step 无法再 claim，重启空转不是 resume |
| 10 判定 | 原终态被恢复读取且无重派可满足；授权重用/重派 FAIL；缺合法窗口/事件为 NOT_VERIFIED |
| 11 STOP | 再批准已消费授权、修改 consumed_at、重置 Step 或派发第二次 |
| 12 人工恢复 | 保留 Action/Approval，按原 lease/恢复路径继续；不得再次决定授权或强行重跑完成的工具 |

#### E：UNKNOWN 只能显式 Reconciliation

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | unknown 场景已获 Action Approval，Action unknown、Agent waiting_reconciliation |
| 2 私有对象 | 同 Action、Step、待提交 revision、独立核实的合成结果和新 reconciliation_ref |
| 3 A/B 角色 | A 留下实际未知结果；B 重启只读恢复，不自动重发；操作者单独负责对账决定 |
| 4 操作者步骤 | before 后先确认重启不派发；核实固定合成结果，再对该 plan-index 显式 reconcile，B 真正 load 后取 after |
| 5 故障与恢复 | 保存导致 UNKNOWN 的实际事件，恢复是人工明确决定；CLI 只适用固定 value:1 合成工具，不适用业务行动 |
| 6 PG 前后快照 | 同 Action unknown→confirmed_status，新 applied 对账记录位于两快照之间，旧历史不变 |
| 7 Host Witness | 真实 resume 绑定新 reconciliation_ref/action_ref；没有未知行动重派 |
| 8 签署来源 | 共用角色，另存独立核实结果与对账操作者原件，不把 CLI 固定结果当外部事实 |
| 9 Reviewer 因果 | 先 UNKNOWN、后 applied 对账、再恢复，全部为同对象；CAS revision 精确 |
| 10 判定 | 显式 applied 且引用/时间准确可满足；无对账自动恢复/重发 FAIL；结果未核实为 NOT_VERIFIED |
| 11 STOP | 对账结果无来源、CAS 冲突、借另一个 Action 的对账、重复执行 |
| 12 人工恢复 | HOLD 未知状态并独立核查，不能重派；CAS 冲突重新只读审计和人工决定，不自动重试批准 |

#### F：同 ProviderPool 的 Permit / Attempt / deferred

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | 同一已启用 AdmissionPolicy/ProviderKey，核对全池活动/近期 Permit 和可用配额 |
| 2 私有对象 | 两个实际竞争的 Step/ModelCall、同 Pool、赢家 Permit/Attempt；均属于获准合成批次 |
| 3 A/B 角色 | 一方真实 reserve 成功，另一方在该池确实满额时发生 Admission deferred |
| 4 操作者步骤 | before 后按批准的有限配额发起竞争，记录实际拒绝、合法释放/延期后状态，再取 after |
| 5 故障与恢复 | 受控配额竞争由操作者见证，非付费 Provider；不靠降低数据库计数或伪造故障 bool |
| 6 PG 前后快照 | 包含池内批次外 Permit，全池容量计算闭合；deferred 方无新 Attempt/Permit/预算增加 |
| 7 Host Witness | 同 pool_ref 的 accepted model_reserve 与 ProviderAdmissionDeferred，关联真实 Attempt/Permit |
| 8 签署来源 | 共用角色，DBA 核对全局池范围与 Fault 指定的 provider_ref |
| 9 Reviewer 因果 | 拒绝时池确实受限，不用刚好同一时刻但另一 Pool 的成功掩盖错误引用 |
| 10 判定 | 真实配额竞争及引用闭合可满足；超额/错误池/延期预算增加 FAIL；缺真实竞争为 NOT_VERIFIED |
| 11 STOP | Permit 重复、池范围溢出、计数不明、占用业务配额或批次外 Handler |
| 12 人工恢复 | 核对有效 Permit、到期/释放与 UNKNOWN，按原 Admission 路径等待，不删 Permit 或重算历史 Usage |

#### G：retry 的同 ModelCall 429 与合法重试

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | retry Run（suite 索引 5），共享 Admission 已启用；不是 model_rate_limited（索引 7） |
| 2 私有对象 | 同 Run/Step/ModelCall/ProviderPool；首 failed Attempt 与下一递增 Attempt，固定 Contract |
| 3 A/B 角色 | A 用 case G 采集并亲自观察首次 Mock 429/defer；该采集进程保持存活；B 可观察合法到期后的预约 |
| 4 操作者步骤 | 首次确认失败/defer 后立即取 before；同采集器实际轮询未到期领取，再等待 available_at 与 cooldown 都到期，取实际下一预约和 after |
| 5 故障与恢复 | retry + case G 的现有 Mock 返回首次 429、retry_after=5s；后续正常继续，不调用真实 Provider |
| 6 PG 前后快照 | before 含 failed Attempt/deferred_retry/将来到期点；after 同 Call 的 index+1，created_at 不早于两种到期点的最大值 |
| 7 Host Witness | 同进程 backpressure 确认和未到期实际 claim 产生 rate_limited，随后 accepted reserve；完整前导事件保留 |
| 8 签署来源 | 共用角色，明确签署的是 Mock 场景真实软件事件，不宣称真实远端 429 |
| 9 Reviewer 因果 | 同 Call/Contract/Pool，延期前不加预算/不造新调用；不能用另一次 normal 请求或重启后历史行替代 |
| 10 判定 | 同调用延迟/合法重试可满足；提前预约/Contract 漂移 FAIL；单尝试耗尽或丢失进程观察为 NOT_VERIFIED |
| 11 STOP | 过早重试、采集器重启、缺 backpressure 来源、事件上限将满、任何付费请求 |
| 12 人工恢复 | 按持久 available_at/cooldown 等待，保留旧 Contract/Attempt；缺证据则新授权重做，不重签历史时间 |

#### H：Summary Generator / Artifact / Consumer / Contract

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | summary_consumption（索引 9）的源证据、generator 和 consumer 依赖有效，确定可恢复边界 |
| 2 私有对象 | 同 Run 的 source/generator/consumer Step、source snapshot、summary request/input、Artifact、view 和两份 Contract |
| 3 A/B 角色 | A 执行生成/持久边界，B 合法接管并以 Verified Summary Consumer 读取同一 Artifact |
| 4 操作者步骤 | 记录 before，在审批指定的持久边界中断本轮进程；B 依原流程恢复 generator/consumer，再取 after |
| 5 故障与恢复 | 仅允许 PID/边界内动作；不能手造 SummaryLink 或用另一个 Run 的摘要替代 |
| 6 PG 前后快照 | 完整 summary_links、Artifact 唯一性、RequestContract digest、Consumer View，旧来源不变 |
| 7 Host Witness | 源/生成器/消费者的实际 resume/reservation，consumer 恢复目标包含在 Fault target_step_refs |
| 8 签署来源 | 共用角色，私有来源材料与摘要披露策略另审；不公开源内容 |
| 9 Reviewer 因果 | Source→Generator Contract→Artifact→Consumer View/Contract 持久闭合，不仅比较一个摘要 hash |
| 10 判定 | 持久绑定与恢复一致可满足；错源/重复 Artifact/Contract 漂移 FAIL；无真实恢复边界为 NOT_VERIFIED |
| 11 STOP | 丢源、未授权正文披露、重复生成或来源身份不能核实 |
| 12 人工恢复 | 保留原 Summary/Contract/Artifact，按已提交状态接续；不重算覆盖历史或手动补消费记录 |

#### I：Cancellation 与真实迟到写拒绝

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | 获准目标存在真实在途调用及有效旧 lease；before 保存该旧 Owner |
| 2 私有对象 | cancel 场景实际 Run/Step/旧 lease；若另测批准后取消，明确另一个已获准 review 对象，不能混引用 |
| 3 A/B 角色 | A 保留真实在途写；操作者取消指定对象，B 独立核对取消已持久化并观察恢复 |
| 4 操作者步骤 | before 后对指定 plan-index cancel，再让获准的原调用实际完成并尝试提交，保存拒绝及 after |
| 5 故障与恢复 | 暂停/恢复原调用的方法须独立审核；快速 Mock 已结束或原进程已死就 HOLD，不生成假晚到写 |
| 6 PG 前后快照 | Run/Step cancelled，旧 lease 对应，Artifact/Approval 不复活，无第二结果 |
| 7 Host Witness | 真正 cancelled_commit，被 LostLease/StateConflict 拒绝；load 拒绝不当作写拒绝 |
| 8 签署来源 | 共用角色，Fault 核对取消与原在途写的实际时序 |
| 9 Reviewer 因果 | 取消持久化早于同旧 Owner 的迟到写；不能用别的取消对象或旧 token 的 C 事件替代 |
| 10 判定 | 实际迟到拒绝且取消不复活可满足；提交成功/新 Artifact FAIL；缺在途窗口为 NOT_VERIFIED |
| 11 STOP | 取消被恢复成 running/succeeded、仍消费撤销授权、旧写成功 |
| 12 人工恢复 | 停本轮旧进程、核对已提交结果/UNKNOWN；按取消和对账原程序处理，不撤销取消或重发 |

#### J：批准的数据库断链、安全停止与恢复查询

| 字段 | 执行与证据记录 |
| --- | --- |
| 1 前置数据库事实 | 核对专用 OID/Marker、目标 Step、批准的客户端连接范围及恢复负责人 |
| 2 私有对象 | 同数据库 identity、环境/批次/Step，记录受影响 Worker 连接与本轮 PID |
| 3 A/B 角色 | A 的执行循环实际遇到数据库异常并停止；恢复后 B 对同库同目标执行实际只读恢复观察 |
| 4 操作者步骤 | before 后由获准 DBA 执行指定断链，确认安全停止；恢复连接/TLS后查询并取 after，核对遗留状态 |
| 5 故障与恢复 | 仅独立审批的专库连接故障；本工具没有断链命令，不执行 SSH/防火墙/远程分区 |
| 6 PG 前后快照 | 中断期间不存在健康空库快照；恢复后区分过期 lease、waiting、UNKNOWN、delayed、终态与 Permit |
| 7 Host Witness | 实际 database_unavailable/safe_stopped，再同目标 database_recovered，误差区间完整落在故障范围 |
| 8 签署来源 | 共用角色，独立 DBA/Fault 确认实际断链与恢复，Host journal 不能自证故障域 |
| 9 Reviewer 因果 | 真实失败→执行循环停止→同库恢复查询；暂时异常或任意观察器不算安全停止 |
| 10 判定 | 实际停止/恢复且数据关系不变可满足；错误库/自动重发 FAIL；缺授权、错误时间或黑洞停止未知为 NOT_VERIFIED |
| 11 STOP | 故障越界、停止确认未知、恢复库身份变化、TLS 失效、查询失败 |
| 12 人工恢复 | DBA 恢复批准连接，先只读审计再授权有限 Writer；保留 UNKNOWN/spent，不改状态、不自动 Seed/Migration |

### 已批准后才可使用的命令模板

以下都是占位模板，本轮不执行。数据库 URL/凭据只能由操作者经环境变量安全提供，不能放
命令行、报告或仓库。审批原件、只读连接授权与真实执行授权各自核对，环境开关不是审批原件。
输出须以排他方式写入新的私有文件，不能用覆盖重定向改写旧 Challenge/报告。身份与预检模板见上文。

```powershell
# 只在独立执行授权核实后；AGENTFLOW_ENVIRONMENT、RC_DATABASE_URL、RC_ADMISSION_POLICY 等由操作者配置。
# --runs 1 的 suite 共十个 Run；--runs 2 为二十个，仍须独立批准且不允许再追加其他批次。
uv run python scripts/verify_rc_staging.py seed --config <config.json> --environment-id <UUID> --allow-staging-execution --scenario acceptance_suite --runs 1 --seconds <1到600> --plan <新私有父目录/plan.json>
$rcContext = @('--config','<config.json>','--v1-report','<本次v1.json>','--plan','<冻结plan.json>')
# 只登记一次，保存输出到新的 challenge.json；原 registry 须为新私有目录。
uv run python scripts/verify_rc_causal.py issue @rcContext --state-dir <原registry> --database-identity-digest <独立核实的数据库identity摘要>
# DB before/after 都须单独只读授权。snapshot 输出未签 payload，不是 SignedEvidence。
uv run python scripts/verify_rc_causal.py snapshot @rcContext --challenge <challenge.json> --read-staging --case <A到J> --phase before
# 每 Host 的 AGENTFLOW_RC_WITNESS_ROOT 与 --records-root 须为相同获准私有目录。
# A 用 probe，其余按核实后的现场阶段用 run；两宿主分别 slot 0/1。此处不提供任何故障命令。
uv run python scripts/rc_host_witness.py probe @rcContext --environment-id <UUID> --challenge <challenge.json> --state-dir <原registry> --records-root <本Host私有root> --case A --target-step-ref <真实目标摘要> --slot <0或1> --seconds <1到600> --allow-staging-execution
uv run python scripts/rc_host_witness.py run @rcContext --environment-id <UUID> --challenge <challenge.json> --state-dir <原registry> --records-root <本Host私有root> --case G --target-step-ref <retry的真实Step摘要> --slot <0或1> --seconds <1到600> --allow-staging-execution
# D/E/I 各自选定的私有索引，命令分别为 approve / reconcile / cancel；不是 Worker 领取过滤器。
uv run python scripts/verify_rc_staging.py reconcile --config <config.json> --environment-id <UUID> --allow-staging-execution --plan <plan.json> --plan-index <unknown的实际索引> --seconds <1到600>
uv run python scripts/verify_rc_causal.py snapshot @rcContext --challenge <challenge.json> --read-staging --case <同Case> --phase after
```

Host 身份必须匹配该 slot；共享 Admission Policy 在两个 Worker 和 seed 时完全一致。G 若直接
运行普通 `verify_durable_soak.py --worker` 而没有 case G Witness，retry 首次是普通可重试错误，
不产生本 Case 要求的 429，不能作为 G 替代。case G 模式才会返回首次 Mock 429，之后恢复。
`probe/run` 正常退出 1 表示 unsigned/NOT_VERIFIED，退出 3 表示 WitnessExecutionRejected 且可能
已授批次外 Lease；不能当作通过。跨批次 Handler 被阻断，不保证批次外 claim 事务未发生。

### 证据目录、冻结与交接

```text
<仓库外私有campaign>/
  approvals/                 # 独立审批/资产/故障/PID/账号/保留原件，不放 bundle
  config.json v1.json challenge.json trust.json operator-materials.json
  seed/plan.json             # seed 时该父目录必须尚不存在；由原 CLI 创建
  registry/issued.json       # 原始单次登记；完成尝试后 consumed.json 一并保留
  source-journals/           # 全部现场原始记录，含不完整 child 事故附件
  bundle/build.json infrastructure.json
  bundle/<A到J>/before.json after.json fault.json binding.json
  bundle/<A到J>/host-0.json host-1.json reviewer.json
  bundle/<A到J>/host-0/{header.json,event-0000.json,...,closed.json}
  bundle/<A到J>/host-1/{header.json,event-0000.json,...,closed.json}
  requests/                  # 只放尚未签署的完整 CausalEvidence envelope，镜像收据路径
  custody/store.json         # 由原工具 init-store 排他创建，不手写
  custody/<ManifestSHA>/manifest.json
  custody/<ManifestSHA>/files/{config.json,v1.json,plan.json,challenge.json,trust.json,registry-issued.json,bundle/,...}
```

上图是目录说明，不是预生成文件。每 Case 包含配置中全部 Worker 的 journal，host 索引对应
配置顺序；binding 精确覆盖这些 sealed journal/process_headers 与实际 fault_record_digest。
禁止把 unsigned-host 缓存、审批原件或未封存 child 混入严格 bundle。
requests 只进入 Prepared 对象；Final 使用签署返回，不复制待签缓存，原请求仍随 Prepared 链保留。

DB/Build/Infrastructure/Fault 方先按已核实材料签署原 v3 收据，原始 bytes 不再改写；Host envelope
由这些来源和完整 journal 准确派生。Prepared 固定已有签名和待签 Host 请求，导出 UNSIGNED
Handoff；返回后保持原 bytes，再冻结 Reviewer 的确切请求；Reviewer 最后签完整集合，再 Final。
若上述来源也需先冻结 Request，可多一层 Prepared，但总链含 Final 最多四对象，不能无限追加。
同路径 Signed Receipt/Request 即使一致也拒绝，已有 Signed Receipt 不重新交接为 UNSIGNED。

所有 freeze/handoff、provenance/export 和最终命令使用
[既有 Custody 手册](rc-independent-acceptance.md#操作者冻结与交接顺序) 的准确模板。
Manifest schema_version=1 固定阶段、Challenge/环境/批次/部署、Trust/Collector、原 registry 绑定、
Prepared 父摘要、文件路径/长度/原始 SHA 与 bundle_content_digest；不认证物理来源。
只从同 store 的已发布完整对象运行 custody-precheck，再最后 custody-verify；不先运行历史 verify。
预检不消费；最终完成的 FAIL/NOT_VERIFIED 也可能消费，须保存退出 0/1/2、report v2 与 registry/spent。
Prepared/Final 中断的 .pending-* 保留，可在有效未消费 Challenge 下原样重试；旧 SHA 半成品按
[人工隔离](rc-independent-acceptance.md#冻结中断重试与人工隔离) 处理，不清除消费、重置 store 或改签名。

### GO / NO-GO、STOP / HOLD / RECOVERY

| 裁决 | 触发条件 | 责任人与下一步 |
| --- | --- | --- |
| GO 提交独立复核 | 全部原件齐全、物理资产/部署/角色已独立核实、时序预算可行、有限 plan 冻结、Readiness 材料检查 GO | 独立审批人复核；仍需另发具体执行授权，execution_authorized=false 不变 |
| NO-GO | 缺环境/故障/只读授权、Signer/资产/DB 原件、窗口预算或目标映射；本轮真实材料即此状态 | 仅提交手册和缺件表供补件，不连接环境、不 issue 现场 Challenge、不创建“已批准”文件 |
| HOLD | 来源/时钟/调用重叠不确定，缺合法恢复边界、临近过期、缺 journal/签名或配额范围无法核实 | 停新 Run/新动作，保留现场；责任方查证，不凭缺失或 hash 猜 PASS |
| STOP | 旧写成功、UNKNOWN/已完成工具重派、取消复活、越权目标、重复 Permit/Artifact、断链停止未知、签名/身份矛盾 | 立即通知获准操作者/DBA，停止本轮 Writer，按批准范围处理本轮进程；不自动修补、迁移或删库 |
| RECOVERY | 明确故障已撤除且 DBA 已核对同库/TLS/部署和全部 lease/UNKNOWN/延期/终态 | 先只读审计，再分别批准租约恢复、显式 Reconciliation 或新的尝试；不清除 consumed/spent，不重发 UNKNOWN |

演练后独立复核全部 A–J、原始 bytes/Manifest/Prepared 链、Challenge issued/consumed、spent、
quarantine intent/done、未完成目录、源 journal、审批/资产/故障原件和最终分层报告，按私有保留
政策归档。备份用于审计，不复制登记发起第二次验收。Windows 目录掉电持久性仍 NOT_VERIFIED。

最终分别记录 SOFTWARE_READY、READY_FOR_OPERATOR_REVIEW、STAGING_EXECUTION_AUTHORIZED、
STAGING_ACCEPTED、PRODUCTION_RELEASE_APPROVED。软件/准备材料最多支持前两项；没有实际
独立材料时第二项仍 NOT_READY。后三项须各自取得正式授权和现场验收，不能由 Readiness GO、
保存证据 PASS、Windows/Linux CI 或本交接包自动晋升。真实 SQL 锁竞争、Provider、Token、
HA/PITR 与生产放行继续单列 NOT_VERIFIED。
