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
