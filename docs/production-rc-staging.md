# Production RC Staging & Evidence Gates v1

新的操作者采集流程见 [RC Host Witness v1](rc-host-witness.md)：identity/preflight/status
与实际执行分离；probe/run 仍需环境 UUID、明确授权、TLS/OID/0008、干净部署、槽位锁及
专用合成批次。默认不连接外部环境。run 只监督自己启动的本机固定子进程，不主动注入远端故障。
日志不包含输入、Prompt、参数、结果或真实 token；缺失、截断、时钟不明和提交确认丢失不会
升级成成功。G 的同调用 429/延期使用 retry fixture 的 case G 采集模式，既有单尝试耗尽 fixture
继续用于终态/备份测试。没有独立签名和 Reviewer 时，只能得到 NOT_VERIFIED unsigned draft。

本轮只验证离线软件门禁。**真实双宿主、真实 Provider、真实 Token Conformance 均为 NOT_VERIFIED**。
软件 CI、单机三进程、逻辑备份通过，不等于目标生产环境已放行。没有自动远程故障操作。

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
summary_consumption。每个私有 plan 单一场景，可分别建立有限的 plan；总 Run 数由操作审批限定，
重复创建 plan 不是额外授权。操作时核对持久化批次标记，不允许拿另一批次的 plan 处理当前 Run。
中途提交确认丢失时停止，先只读审计，不盲目重新 seed。
Worker 执行循环最长 3600 秒；合成执行阶段 deadline 最长 600 秒（默认 60），另有预检和有界数据库/
清理耗时。Worker 正常退出只表示本轮循环结束，不代表所有 Run 成功或发布门禁通过。
槽位使用独立 PG session advisory lock，不替代 Step lease/fencing；监测连接丢失时停止本轮 Worker。
只允许直连 PostgreSQL 或经独立核对的 session pooling；transaction pooling 不支持此 session 锁。
v1 不自动发现中间代理，路由/池模式未知时不得演练；网络黑洞下还须由 DBA 验证 TCP 存活超时。
不会启动远端进程、改防火墙或自动删库；原 Runner 最多五秒取消清理不变，不能强杀已进入驱动的线程。

## A–J 验收记录和停止条件

新增的独立验收入口见 [RC Independent Acceptance v2](rc-independent-acceptance.md)：
v1 仍拒绝全部非预检 PASS；v2 绑定原报告、随机 Challenge、公钥角色与人工审核。
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
| D | review 批准并完成后重启；批准 consumed_at/Action ID/幂等键不变，再批准不产生派发 |
| E | unknown 批准并挂起；重启 B 不派发；操作者核实合成结果后显式 reconcile，再恢复 |
| F | 在相同 ProviderAdmissionPolicy 下两宿主争抢；核对 Permit 唯一键/配额及重启后有效计数；不使用本地 Counter 判断 |
| G | model_rate_limited + 已启用共享 Admission；再 seed 正常请求，检查 cooldown/available_at 到期前预算不增加；retry 的同 model_call_id/递增 attempt 不能提前 |
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
