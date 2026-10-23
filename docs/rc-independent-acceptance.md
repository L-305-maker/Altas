# RC Causal Acceptance v3

新的生产验收入口是 `scripts/verify_rc_causal.py`；`verify_rc_acceptance.py` 明确保留为
**历史 v2** 入口。v3 不修改 Agent Core、数据库状态或迁移，Alembic head 仍为 0008。
两个入口都只验证保存证据，`exit 0` 不是生产发布许可。离线报告的实时数据库、Provider、Token
和生产放行状态始终为 NOT_VERIFIED。

## 为什么必须使用 v3

完整合成签名回归已复现 v2 的两个 P1 假阳性：A 中宿主一领取 X、宿主二对 Y 竞争失败；F 中
不同对象的预约与任意新增 Permit 可以组合成 PASS。v2 只计操作、参与宿主和局部数据库变化，
不能证明这些事实属于同一次竞争。归档 v2 JSON、签名消息、报告和消费登记全部保持原身份；
旧 PASS 只代表旧验证规则，不得晋升为 v3 PASS。没有回填 Witness，也不修改历史报告。

## v3 契约与签名

`CausalChallenge(evidence_version=3)` 保留原环境、部署、数据库、批次、v1 预检摘要、256-bit nonce
和最多一小时期限。`CausalEvidence`、`CausalSignedEvidence`、`RCCausalAcceptance(schema_version=3)`
使用单独的类型和验证入口。签名消息是 `Altas.RCCausalEvidence.v3\n` 加规范 UTF-8 JSON；v2 的
`Altas.RCIndependentEvidence.v2\n` 不可认证 v3。规范化仍为键排序、紧凑编码、保留 Unicode、
拒绝 NaN/Infinity；不修改任何 ModelRequestContract 身份。

不可变 `RCCausalWitness` 继承有限操作枚举及 outcome/time，另绑定：

- environment、batch、case、causal_group_id、固定签名宿主身份；
- target_step_ref，以及适用的 Action、ModelCall、Attempt、Permit、Provider、Approval、Reconciliation 引用；
- 不可逆匿名 lease digest，不输出原 token；
- 完整 before/after 快照摘要、独立 fault 收据摘要及 Witness 自身摘要。

Witness 摘要覆盖全部序列化字段（包括默认 null）；causal_group_id 是 Challenge 摘要、case、两份
快照摘要及 fault 收据摘要的规范 JSON 摘要。Fault 明确列出最多三个受测 Step、可选配额池、
最多 60 秒的竞争间隔；签名 envelope 起止时间是本次故障演练窗口。每条操作必须同时落在该窗口、
宿主 envelope 和数据库观察区间内。A/F 的默认竞争间隔为五秒，不通过把窗口任意扩大来拼接演练。

独立 Reviewer 签署该 case **全部非 reviewer 收据**加全局 build/infrastructure 的排序摘要集合；
其签署时间不早于全部被审阅收据结束。每 case/host 只接受一份有界宿主收据；相同底层事件即使换 case
包装也拒绝。跨 Challenge/batch、角色混淆、篡改、未登记公钥、签名域混用、过期、重复消费均拒绝。
公钥注册、固定文件 SHA、独立角色、基础设施资产映射和私有 O_EXCL/fsync 消费登记沿用 v2 信任边界。
签名仍只是受信任角色的声明；它不能自行证明物理宿主独立或真实网络中断。

## A–J 的关联规则

| Case | v3 必需的同一对象因果链 |
| --- | --- |
| A | 两个宿主竞争同一 Step；READY→RUNNING；winner 的 lease digest/截止时间等于数据库，loser 在有效租约内明确 rejected/unavailable，竞争不超过指定间隔；重叠 accepted lease 为 FAIL |
| B | 原租约持有宿主的 worker_exit、安全停止、同一 Action INFLIGHT→UNKNOWN 和 waiting reconciliation；退出收据必须匹配原 lease digest，且退出发生在该租约失效前 |
| C | 同 Step 原 lease、新 lease 与另一宿主 accepted claim；原 lease 的晚到提交在接管后明确 LostLease；连接错误不算拒绝；旧租约尚有效即接管为 FAIL |
| D | resume 明确引用原 Action 与已消费 Approval；ToolBinding、幂等身份、批准事实和完成结果保持稳定，无再次派发 |
| E | 对应旧 UNKNOWN Action、唯一 applied 对账、明确确认时间及随后 resume；未 applied 或其他 Action 的记录不足 |
| F | 同 Provider pool、同有界竞争窗口；winner 的真实新 Call/Attempt/Permit 及预约时间全部相符；另一宿主明确 ProviderAdmissionDeferred 安全拒绝且无新 Attempt；拒绝时持久全局配额已满，包含其他 Run 的 Permit |
| G | 已确认失败的旧 Attempt、同一 Call/Step/Provider、持久延期和 cooldown；新 index+1 Attempt 在两个 deadline 后预约；rate_limited 收据是对该已确认失败/延期事实的观察，不伪造发生在 before 之前的 HTTP 时间 |
| H | 同 Run 的 source→generator→artifact→consumer view；原 SummaryInput、来源快照及请求摘要与真实 Generator/Consumer Contract 字段交叉核对，不能组合任意两个摘要 |
| I | 被取消 Run/Step 就是原 lease 晚到结果遭 LostLease/StateConflict 的对象；其他 Run 的拒绝不作证明 |
| J | 同环境、数据库身份、Step 和签名故障窗口内，database_unavailable/safe_stopped 后有明确 database_recovered，随后真实只读观察；只有不可用收据不能 PASS |

数据库关系包含真实 ModelCall 归属、Action 的 ToolBinding/幂等摘要、审批消费、applied 对账及
Summary 来源/Contract 链；旧关系不得消失或移到另一个 Run。原 revision、预算、UNKNOWN、终态、
Artifact、配额和迟到提交不变量仍执行。明确可信矛盾输出 FAIL；缺少目标、关系、窗口、宿主、故障或
独立审核为 NOT_VERIFIED。不能将缺证据当成功，也不把合法人工等待当成系统故障。

## 只读采集与操作者步骤

v3 与 v2 复用同一个采集事务。PG 使用 REPEATABLE READ / READ ONLY、statement_timeout=5s、
lock_timeout=2s；无 FOR UPDATE、DML 或修补。实际会话仍经原 AgentStore reader 和 Summary/Consumer
校验。最多 20 Run/100 Step，每类 2000 子记录、16 MB 完整会话、2 MB 脱敏快照、30 秒 deadline；
100 收据和总 8 MB 验收输入上限不变。全配额池查询包含批次外所有 Permit，超限拒绝，不将未扫描数据
当作不存在。数据库不可用直接安全拒绝，不输出健康空快照。不同请求不是同一个全库一致快照。

先独立批准 staging、资产清单、操作员、故障窗口、SELECT-only 角色和发布信任根，并核实时钟同步；
无法可靠关联数据库与宿主 UTC 时间时保持 NOT_VERIFIED。以下命令不会
自动启动远端宿主、注入故障或发送付费请求。参数沿用下方 v2 操作说明，但使用新的独立入口：

```powershell
uv run python scripts/verify_rc_causal.py issue --config <config.json> --v1-report <v1.json> --plan <plan.json> --database-identity-digest <已独立核实摘要> --state-dir <新的私有v3目录>
uv run python scripts/verify_rc_causal.py snapshot --config <config.json> --v1-report <v1.json> --plan <plan.json> --challenge <v3-challenge.json> --case A --phase before --read-staging
uv run python scripts/verify_rc_causal.py verify --config <config.json> --v1-report <v1.json> --plan <plan.json> --challenge <v3-challenge.json> --state-dir <同一私有v3目录> --trust <已批准公钥文件> --trust-sha256 <独立渠道固定SHA> --evidence <签名收据.json>
uv run python -c "import sys,json; sys.path.insert(0,'scripts'); from rc_causal import CausalEvidence; print(json.dumps(CausalEvidence.model_json_schema()))"
```

issue 输出应保存在新 Challenge 文件；snapshot 仍为未签名采集结果。受保护的外部签名服务分别签署
数据库、构建、宿主、基础设施和故障证明，最后由独立 Reviewer 核查实际原始记录。私钥、原 token、
计划和连接 URL 不进入公开报告或 CI Artifact。全部 A–J 需一次汇集后验证；部分集合退出 1，明确
矛盾/拒绝退出 2，完整保存证据通过退出 0。验收后即消费 Challenge，补证需重新批准新演练。
旧登记不迁移、不删除重用；v2 Challenge 不能用于 v3。

数据库负责持久关系，宿主负责操作及 lease 身份，基础设施方负责物理故障域，故障执行方负责受保护
故障记录，Reviewer 负责独立核实整个集合；Provider 的真实身份/付费 Smoke 另行授权，不能由这些
收据替代。真实双宿主、网络分区、Provider、exact Token Counter 与 HA/PITR 继续 NOT_VERIFIED。
本地临时 PostgreSQL、单机三进程 Soak、逻辑备份及软件签名测试不能认证这些生产事实。

---

# RC Independent Acceptance v2

本层验证已保存的演练证据，不是生产发布授权，也不改变运行时数据库。RC v1 的非预检 PASS
禁令保持原样；旧报告没有被升级。新 `RCIndependentAcceptance(schema_version=2)` 引用原始
v1 报告的规范化 Digest，逐项输出 A–J 的 PASS / FAIL / NOT_VERIFIED。

PASS 的准确含义是：**本次 Challenge 下，受信任的独立身份提交了完整证据，离线逻辑核查通过，
且独立审核人已批准相同证据集合**。它不表示验证器刚刚连接过目标数据库，也不是签名证明了物理事实。
`live_database_status`、`provider_acceptance`、`token_conformance` 和 `production_release_status`
在该离线报告中始终为 NOT_VERIFIED。正式发布仍由目标部署的发布负责人依据独立验收授权决定。

## 来源与信任边界

| 来源 | 可核查内容 | 不能替代的证据 |
| --- | --- | --- |
| database | 一个 REPEATABLE READ / READ ONLY 快照内的真实 Step、完整会话验证结果、预算、Action、Attempt、Permit、审批消费时间、已应用对账时间和 Artifact 摘要 | 两次快照之间所有外部副作用、物理断链是否发生 |
| build | 已部署 SHA、应用版本、Core/策略/工具/用量映射摘要和请求版本 | 部署收据必须由独立部署身份签署，不能由待测 Worker 自证 |
| host | 受保护执行器采集的固定枚举操作结果、匿名目标和时间；领取租约截止时间；LostLease / StateConflict 安全码 | JSONL 不是数据库账本；同一个人复制两个 host digest 不构成独立宿主 |
| infrastructure | 独立资产管理方确认的 host digest 到物理故障域摘要的映射、受保护资产清单 Digest | 机器 ID、VM 名称或签名本身不证明宿主物理独立 |
| fault | 经过单独授权的指定故障阶段、时间、受保护原始记录的 Digest | 摘要不能证明断网；审核人必须核对实际演练记录和授权 |
| reviewer | 对上述收据集合的独立审核决定与集合 Digest | 不能靠上传方自行登记公钥、自填批准来获得发布权限 |

注册由发布安全负责人完成：每个角色使用不同的 Ed25519 公钥；每个 Worker 公钥固定绑定一个
host digest。注册时独立核验人员/服务身份、资产来源与职责分离。多把密钥由同一操作者控制的事实
无法由密码学自动发现；注册审核必须阻止这种角色冒充。仓库不内置生产信任根，不提供生产私钥生成或签名入口。
合成签名密钥只存在离线测试内存中。

Ed25519 使用已维护的 [cryptography 实现](https://cryptography.io/en/50.0.0/hazmat/primitives/asymmetric/ed25519/)，
是运维验证脚本的显式开发依赖，不加入 Agent Core 的运行时依赖。
签名消息为 `Altas.RCIndependentEvidence.v2\n` 加 `AcceptanceEvidence.model_dump(mode="json")`
的规范 UTF-8 JSON：键排序、无额外空白、保留 Unicode、禁止 NaN/Infinity。与模型请求身份复用同一
`canonical_json`，没有修改 RequestContract v1/v2 的序列化规则。
证据 SHA-256 是同一规范 JSON（不含签名）；审核集合摘要是排序后的非 reviewer 收据 Digest 列表的摘要。
每个 case 的审核集合仅包含该 case 的收据及全局 build/infrastructure 收据，不能漏掉失败收据或混入另一个 case。

可信公钥文件为 `AcceptanceTrust(version=N)`，最多 16 个身份；字段包括公钥原始 32 字节的小写十六进制、
`key_id=request_digest(public_key_hex)`、角色、host 绑定和有效期。版本必须经过独立审批，文件原始字节的
SHA-256 通过独立发布渠道固定到验收命令。不要使用证据上传包附带的 trust 文件或摘要作为信任根。
轮换需递增版本、独立审核新旧有效期及角色、更新渠道固定摘要，并保存旧公钥供历史只读核查；
撤销或回滚到旧版本必须明确审批，验证器不会自行信任任何更“新”的上传文件。

## Challenge 与防重放

Challenge 由验收机生成 256 位随机 nonce，有效期最多一小时。签名绑定 environment UUID、部署 SHA、
应用版本、Alembic revision、数据库身份摘要、完整私有计划摘要、v1 报告摘要和 nonce。
数据库摘要覆盖环境、OID、连接端点、数据库名，不含用户名或密码；实时采集还必须通过既有 TLS、
管理员 Marker、最小权限和部署预检。OID 或摘要本身都不是服务端身份验证。

验收机创建新的 0700 私有目录，原子排他保存 `issued.json`；验证完成后排他创建并 fsync
`consumed.json`，成功创建之前不输出验收报告。同一 Challenge 并发或跨进程再次消费会拒绝。
证据重复、过期、签名/摘要篡改、跨环境/构建/数据库/批次复用、未登记或错误角色的签名均拒绝。
结构合法但来源不足的报告保持 NOT_VERIFIED，不因“所有上传 bool 为 true”而通过。

此目录是**验收机的私有防重放登记**，不是第二套运行时事实账本，不写 PostgreSQL。
必须由发布控制方保留在可靠本地磁盘，禁止上传方选择/复制/替换目录；不得删掉 tombstone 后重用 Challenge。
目录丢失或消费写入结果未知时，旧 Challenge 不可用于发布，重新审批新的演练。
通过结构/签名校验后，即使结果不完整或 FAIL 也消费本次 Challenge；不要用一次挑战不断补换证据。
仅调用纯函数 `verify_acceptance()` 不执行防重放消费，不是正式验收入口。
Windows 私有目录要求 Python 3.12.4+；文件系统需支持本地排他创建和 fsync。

## A–J 的可检查不变量

每项都要求：同一批次的前后数据库快照、实际部署收据、两台有非空执行收据的宿主、不同物理故障域的
基础设施证明、对应故障阶段收据及独立审核。故障及操作时间必须位于前后快照之间；目标摘要必须来自
对应 Step/Action，不能拿另一 Run 的“拒绝”补足当前场景。观察不足为 NOT_VERIFIED；发现矛盾直接 FAIL。

| 项 | 必要故障阶段 | 数据库与执行收据的交叉核对 |
| --- | --- | --- |
| A | lease_competition | 同一 Step 从 READY 到真实运行中租约；两宿主竞争领取；成功领取的租约区间不能重叠 |
| B | worker_exit_inflight | 原 INFLIGHT Action 变 UNKNOWN，Step/Agent 进入 waiting reconciliation；无自动重派 |
| C | partition_before_takeover | 原租约已被新租约替代；同 Step 的旧提交明确得到 LostLease，不能把连接异常当作拒绝证明 |
| D | consumed_approval_restart | 已消费批准时间、Action/幂等身份、已确认结果稳定；恢复收据不包含再次派发 |
| E | unknown_before_reconciliation | 旧 UNKNOWN 经具有 applied 的人工对账解决；对账时间在两快照之间；恢复不自动重派 UNKNOWN |
| F | shared_admission_competition | 两宿主预约，对应本批次新增 Attempt/Permit；全局共享配额（含其他 Run）按唯一 Attempt、并发区间及请求窗口重算 |
| G | rate_limit_before_deadline | 已确认失败、未来 available_at；新 Attempt 复用逻辑调用，创建时间不早于持久 deadline/cooldown |
| H | summary_recovery | Generator Artifact 和 Consumer View 都经过既有验证；旧 Attempt 的 Contract/Projection 摘要稳定，Artifact 唯一 |
| I | cancel_before_late_result | 取消后不能新增 Attempt/Artifact、改变终态或接受迟到提交；拒绝收据须是 LostLease/StateConflict |
| J | database_disconnect | 独立故障记录、执行器数据库不可用时安全停止、恢复后真实只读快照；合法 waiting/UNKNOWN 不当作损坏 |

共通检查还包括 revision/预算不倒退、旧记录不消失、终态和等待状态一致、WAITING 无 INFLIGHT、
旧 UNKNOWN 模型不创建新尝试、Action 完成后不派发、Permit/Artifact 不重复。摘要本身不能检测两个快照之间
未留下持久痕迹的工具重复调用，因此外部执行收据的完整性和独立审核是必要条件，不宣称 exactly-once 副作用。

采集对完整会话调用现有验证 reader（`lock=False`），读取 Summary 及 Consumption 的原始绑定，
不补写、不 reconcile、不续租、不重发。单个批次最多 20 Run、100 Step、每类子记录最多 2000、
完整会话累计最多 16 MB、单份脱敏快照最多 2 MB、30 秒采集 deadline；数据库语句 5 秒、锁等待 2 秒。
同配额历史超过 2000 Permit 时拒绝截断，使用新建的专用演练数据库或更小批次，不删除旧事实腾空间。
验证最多 100 份收据、单文件 2.1 MB、累计规范数据 8 MB；拒绝重复 JSON 键和非法数值。
这些是有界验收，不能把缺失的历史当作已扫描。SQLite 仅用于离线协议回归，不证明 PG 行锁。
快照明确记录实际 database_backend；SQLite 或 unverified 不能满足 PostgreSQL 证据门槛。
宿主收据与基础设施映射还须与预检的 Worker 清单相符；A/F 必须有两台实际参与 claim/model_reserve。

## 操作者执行步骤（本次未自动执行真实 staging）

1. 发布负责人另行批准专用 staging、两宿主、有限故障窗口与操作员；DBA 配置 0008、verify-full、
   Marker/OID、SELECT-only 验收角色及既有 statement/lock timeout。按 staging 文档收集实际构建/宿主收据，
   独立资产方核对物理故障域并注册公钥。Provider Admission 策略须在两 Worker 上完全相同且已启用。
2. 使用既有独立执行授权 seed；`acceptance_suite` 按固定顺序生成 10 个合成 Run 的同一批次，`--runs 1`
   是一套（最多两套/20 Run）。此命令不自动启动宿主、不注入故障，不是 A–J 完成证明。

```powershell
uv run python scripts/verify_rc_staging.py seed --config <config.json> --environment-id <UUID> --allow-staging-execution --scenario acceptance_suite --runs 1 --seconds 60 --plan <新私有目录/plan.json>
uv run python scripts/verify_rc_staging.py preflight --config <config.json> --environment-id <UUID> --read-staging > <v1.json>
# 数据库身份摘要由 DBA/发布方核对端点、OID、Marker 后在可信验收机确定；不是由上传包自行声明。
uv run python scripts/verify_rc_acceptance.py issue --config <config.json> --v1-report <v1.json> --plan <plan.json> --database-identity-digest <已核对摘要> --state-dir <新私有Challenge目录> > <challenge.json>
```

数据库摘要计算使用 `rc_acceptance_db.database_identity(config, make_url(url))`；`url` 只从受保护环境取，
只导出摘要，禁止打印 URL。`issue` 只接受最近一小时的有效 v1 预检报告，不连接数据库。
私有计划、Challenge 状态及原始 token 诊断记录均不进入普通 CI Artifact。

3. 两宿主分别沿用 `verify_durable_soak.py --worker ... --slot 0/1` 的全部授权和预检参数。
   宿主故障和数据库断链由操作者另行执行，仅针对获准本轮进程/专用连接。按 A–J 表采集前后快照。

```powershell
uv run python scripts/verify_rc_acceptance.py snapshot --config <config.json> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --case B --phase before --read-staging > <B-before-unsigned.json>
# 仅批准本计划中 crash 场景（索引 3）；索引 2=review，4=unknown，6=cancel，8/9=summary/consumer。
uv run python scripts/verify_rc_staging.py approve --config <config.json> --environment-id <UUID> --allow-staging-execution --plan <plan.json> --plan-index 3 --seconds 60
# 在获准故障点后，用相同 snapshot 命令 --phase after 收集；不能自动把 waiting 改成成功。
```

前后快照需要围绕实际故障点采集（例如 B 的 before 必须已是 INFLIGHT）。以上示例命令的执行顺序不是
自动故障脚本；尚未到达指定持久状态时停止，不能凭命令返回 0 判断场景通过。
混合计划的审批/对账/取消必须选 `--plan-index`，避免对无关场景批量操作；原单场景入口仍兼容。

4. 独立受保护采集器将上述 payload、实际起止时间、Challenge 和登记的 key_id 组成 `AcceptanceEvidence`。
   build/infrastructure/host/fault 也使用各自固定 schema。公有 JSON schema 可离线导出：

```powershell
uv run python -c "import sys; sys.path.insert(0,'scripts'); import json; from rc_acceptance import AcceptanceEvidence; print(json.dumps(AcceptanceEvidence.model_json_schema()))"
```

   签名服务在受保护环境对 `signing_bytes(evidence)` 签 Ed25519，输出 `SignedEvidence`（evidence、
   evidence_digest、64 字节签名十六进制）；私钥不进入此仓库或输出。Host 操作只允许固定枚举，不含原始 token。
   目标引用为 `request_digest((str(environment_id), checked_plan(plan, environment_id), parts))`；
   parts 为 `("step", StepRow.id)`、`("action", session_id, action_id)`，数据库断链目标则是 Challenge 的数据库摘要。
   这些关联由受保护执行器从实际私有计划和数据库产生，不能用普通日志里的自填成功状态替代。
   独立审核人查验真实资产、授权与原始故障记录，核对 Digest，再签每个 case 的证据集合摘要与 approved/rejected。
5. 验收机从独立渠道取得 trust 文件固定 SHA，明确列出有界证据文件：

```powershell
uv run python scripts/verify_rc_acceptance.py verify --config <config.json> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --state-dir <原私有Challenge目录> --trust <受控trust.json> --trust-sha256 <独立渠道固定的文件SHA256> --evidence <build.json> --evidence <infrastructure.json> --evidence <B-before-signed.json> --evidence <B-after-signed.json> --evidence <host0-B.json> --evidence <host1-B.json> --evidence <fault-B.json> --evidence <review-B.json>
```

   上例只含 B，因此其他项为 NOT_VERIFIED；正式提交需在一次验证前汇集 A–J 全部收据。
   退出 0 表示 A–J 保存证据全部接受；1 是缺失；2 是矛盾、伪造或验证失败。所有公开错误均为固定安全码。
   离线 verify 不读取任何数据库 URL，不执行远端连接。它不输出 CROSS_HOST 实测 PASS 或实时数据库已核查。
   数据库采集失败不输出健康空快照；先停演练，保留现场，由独立操作者核查。

## Provider、Token 与恢复边界

`verify_live_provider.py --report-version 2` 明确分离 request_smoke_status 与 provider_identity_acceptance。
同样保留 `--allow-paid-request`、独立授权环境变量和专用 Key 三项；无授权请求数为零。
响应模型缺失为 NOT_VERIFIED，匹配为 PASS，不匹配为 FAIL。请求失败/429/UNKNOWN 不发第二请求；
durable_attempt_status、usage_record_status、artifact_status 分开报告。Usage 不存在时验证的是已保存的
unavailable 事实，不伪造 token=0。v1 CLI 默认输出继续兼容；v2 字段不被添加到历史报告重新解释。
v2 仅在全部必要检查 PASS 时退出 0，身份或持久提交缺失退出 1；手动 RC Provider Job 显式选择 v2，默认仍不执行。
无真实调用时，软件 MockTransport PASS 只能证明入口契约。即使 v2 Smoke PASS，也不能替代独立 Provider
身份审核或证明实际权重版本。RC 离线验收不会将 Provider Gate 自动晋升。

LIVE TOKEN CONFORMANCE NOT VERIFIED。需要固定托管模型修订、完整 framing/工具 Schema/特殊 Token
规则、不可变 tokenizer/template/framing 资产和经审核的 exact/upper_bound 证明；本次没有新增 DeepSeek Profile。
逻辑 pg_dump/restore PASS 也不代表 HA、PITR 或零丢失。发布负责人需另行批准目标环境恢复、真实双宿主和
必要的付费 Smoke；当前软件 CI 通过与正式生产放行是不同决定。
