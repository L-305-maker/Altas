# RC Host Witness Collection — v2 Hardening

核实日期：2026-10-23。此入口收集真实 RC 执行边界的脱敏观察，默认不执行外部
操作、不签名，也不宣布生产放行。Core、数据库事务、调度器和 0001–0008 均不变。

## 来源与信任边界

`scripts/rc_host_witness.py` 的 `ObservedStore` / `ObservedAgents` 只包装实际调用的
返回或异常，在事务结束后收集只读数据库快照。工具入口在合法 INFLIGHT 预约后观察
实际 invocation；不提供导入“claim accepted”“LostLease”等任意事件的 CLI。
数据库仍是状态权威，文件失败不改变调用结果、不重试工具、不使旧 token 获得权限。

每个新建私有 journal 绑定环境、Challenge、batch、case、host、部署 SHA、实际采集器
文件摘要、进程 PID/随机 nonce 和初始快照摘要。事件只含固定类型、哈希引用、安全码和
时间。租约 token 只在本进程内存中用于实际调用；不写入 journal。Prompt、Task 输入与
依赖、工具参数/结果、actor、证据正文、连接 URL、API Key 均不输出。

| 观察 | 实际来源和限制 |
| --- | --- |
| claim accepted | Store.claim 返回 Task，且真实 Step 的 token 摘要、运行状态和租约匹配；时间来自数据库租约授予时间 |
| claim rejected | 实际 claim 返回 None，随后只读快照确认指定 Step 的有效租约；SKIP LOCKED 返回时若赢家尚未提交，则没有足够证明，不生成拒绝 |
| Action dispatch / UNKNOWN | 实际工具入口及 AgentStore 成功提交后的持久状态；UNKNOWN 记 unavailable，不记 confirmed failure，不重派 |
| model reservation / Admission deferred | reserve_model_attempt 的真实返回，核对 Attempt、Permit、Provider 和预算；load 不补造预约成功 |
| stale / cancelled commit | 实际写方法抛出 LostLease；load 的同类异常只记 resume rejected，不能充作提交拒绝 |
| rate limited / deadline | 本宿主成功确认的带 backpressure 失败，加上随后实际未领取与同 Call/Attempt/Pool 的延迟事实；单独看池冷却不充分，重启丢失观察时保持未验证 |
| resume | 本租约第一次实际 load，可绑定已消费审批或 applied 对账；完整 ToolBinding、幂等键和 Summary 来源由 v3 数据库快照验证 |
| Worker exit | 本轮启动的本地子进程；父进程在它存活时核对私有 journal 的 PID/nonce、真实 INFLIGHT Action 和 lease，再观察其实际退出；过期或缺证据不补写 |
| database unavailable / safe stop / recovered | 实际数据库异常、确实停止的执行循环、同一数据库的成功只读查询分别记录；暂时异常不等于已经安全停止 |

COMMIT 后确认丢失只记 database unavailable；恢复 load 读取事实，不将其改称当前进程
观察到的预约成功。计数、授权、Summary/Consumer、Action 和预算都不由采集器修改。

Hash chain 和 journal seal 只检测缺失、重复、截断、顺序和内容变化，**不能认证文件的
创建者**。拥有操作系统权限的人可以重写整条链；必须依靠受保护部署、独立签名器与
Reviewer 核对实际来源，不能把一份格式正确的 JSON 当成物理事实证明。
父进程退出观察另保存 source_record_digest，绑定它实际读取的子进程 header 和校验前缀，
供签名器对照私有现场；此字段不改变 v3 operation 格式。LostLease 异常还须有实际失去
有效租约的数据库依据；有效当前租约下伪造的异常不能生成拒绝收据。

## 安全执行顺序

1. 发布负责人另行批准专用 staging、两宿主、有限 Run 数和故障窗口。部署同一干净 main
   SHA，并重新生成部署清单：runtime digest 现在包含采集器代码。DBA 核对 TLS
   verify-full、数据库 OID/Marker、0008、最小权限及 statement/lock/TCP 超时。
   槽位 session 锁要求直连或已审核的 session pooling，不支持 transaction pooling。
2. 使用现有 `verify_rc_staging.py` 生成最多 20 个合成 Run 的私有 plan 和有效 v1 预检，
   通过 `verify_rc_causal.py issue` 注册 v3 Challenge。保护原 registry，不能复制登记或
   删除 consumed tombstone。不同主机 registry 的安全分发、冻结和最终消费由发布方负责，
   不是一个跨主机线性一致的消费服务。完成离线验收后不得继续采集本 Challenge。
3. 在每个获准 Worker 主机预建仅服务账号可访问的私有目录，把它同时设为
   `AGENTFLOW_RC_WITNESS_ROOT` 与 `--records-root`。连接只从受保护的
   `AGENTFLOW_RC_DATABASE_URL` 提供；执行还要求
   `AGENTFLOW_RC_STAGING_AUTHORIZATION=synthetic-staging-only:<环境UUID>`。
   不在命令行或报告中放 URL。先运行 identity / 只读 preflight，再执行以下受控入口。

```powershell
uv run python scripts/rc_host_witness.py identity
uv run python scripts/rc_host_witness.py preflight --config <config.json> --environment-id <UUID> --v1-report <v1.json> --plan <plan.json> --read-staging
uv run python scripts/rc_host_witness.py status --config <config.json> --environment-id <UUID> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --state-dir <registry> --read-staging
# 每台主机的 slot 必须与受保护清单一致；target-step-ref 来自该批次 v3 数据库快照。
uv run python scripts/rc_host_witness.py probe --config <config.json> --environment-id <UUID> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --state-dir <registry> --records-root <private-root> --case A --target-step-ref <摘要> --slot 0 --seconds 30 --allow-staging-execution
uv run python scripts/rc_host_witness.py run --config <config.json> --environment-id <UUID> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --state-dir <registry> --records-root <private-root> --case B --target-step-ref <摘要> --slot 0 --seconds 60 --allow-staging-execution
```

`probe` 会真正领取一次任务，属于执行操作，不是只读检查；返回 None 本身不证明竞争。
`run` 只启动本轮本机的固定 Worker 子进程，沿用原授权、预检和槽位锁。父进程仅观察退出，
不发远端命令、不自动模拟断网。超出本轮时限或操作者中断时只清理自己启动的子进程，
此种不完整采集不作为故障验收通过。正常 Worker journal 位于 child-*，父进程的 host-*
journal 只含它亲自观察到的退出。被强退子进程没有 seal，不能补成完整日志。

Store.claim 仍是全局领取，未增加第二调度器。入口检查之后，每个真实返回 Task 都须通过
`HostCollector.authorize`，才能离开 `ObservedStore.claim` 进入 Worker 的 Handler。
校验绑定冻结 plan 的 Run、batch、scenario、真实 Step UUID、完整 StepSpec、允许 Handler、
Task 输入及当前 token/attempt；快照与执行共用 `plan_run` 的批次定义。原 plan 的外部修改
不会改变授权集合，采集文件失败也不会跳过授权。普通 run 和 claim-only probe 使用同一边界。

**残余限制：批次外 Lease 仍可能已被 Core 授予并提交。** 新检查保证批次外 Handler 零执行，
不声称该事务没有发生，也不取消、完成、删除或自行归还 Task。此时进程退出 3，报告
`WitnessExecutionRejected`、`lease_may_be_held=true`、`operator_review_or_lease_expiry`；
尽可能写入只含哈希引用的 `execution-rejected.json`，journal 无法封为完整证据。
操作者应冻结其他 Writer，核对获租对象并按既有租约到期/恢复程序处理；没有 RLS 或原子批次
claim API，专库隔离仍然必要。该风险没有彻底消除。

## 分阶段 A–J

* A：冻结其他 ready Step，围绕同一目标真实 probe；保存唯一 accepted 与有有效租约依据的
  rejected。若竞争窗口内观察不足，停止并使用新授权、新 Challenge 重做，不填写成功事件。
* F：两宿主配置同一 Admission Policy/ProviderKey，捕获真实 Permit/Attempt 与真实 deferred；
  v3 快照包含池内批次外 Permit，溢出拒绝、不截断。不用本地 Counter 推断共享配额。
* B/C：操作者只终止本轮获准子进程或中断其专用连接。B 的 before 已为 INFLIGHT，父进程
  必须在 lease 到期前观察退出；另一 Worker 按原规则恢复 UNKNOWN。C 需要原 Worker
  恢复连接后真正提交被拒绝；已死亡进程不能凭 JSON 生成一次晚到提交。
* D/E：通过既有明确授权的审批/对账入口操作具体 Action。D 的 before/after 要覆盖同一已消费
  授权及已完成 Action 的恢复读取；终态 Step 不能重新 claim，因此缺少合法恢复窗口时未验证。
  E 的 before 是 UNKNOWN，after 必须有同 Action 的 applied 对账和真实恢复读取。
* G：使用 acceptance suite 的 retry Run（索引 5）和启用的共享 Admission。采集模式 case G
  对第一次 Mock retry 产生确认的 429，之后保留原 model_call_id 正常继续；仍非真实 API。
  先在失败/defer 后取 before；同一采集进程继续观察未到期的实际 claim，之后取合法重试。
  旧 model_rate_limited fixture 仍是单尝试耗尽，用于备份/终态测试，不能替代此同调用链。
* H：围绕实际 Summary Generator、Artifact、Consumer View/Contract 链取快照；只有摘要身份
  不足以证明宿主恢复事件。I 要求同取消对象的真实晚到写拒绝。J 要求操作者授权的实际断链，
  safe stop 与恢复查询同库同目标；时间无法核实时保持未验证。

所有故障收据由独立操作者记录并受保护签署。没有实际故障，不生成 fault confirmation。
两宿主即使有日志，也必须分别通过独立资产方核验故障域；本机多进程不等于跨主机演练。

## 封存、独立签名与防重放

每 journal 最多 200 事件、1 MB 事件/头总额、单项 16 KB；每次采集 1–600 秒，数据库读取
沿用有界快照及超时。文件 O_EXCL 创建、私有目录、fsync，不覆盖已有记录；禁止上传原始
目录到普通 CI Artifact。POSIX 检查权限，Windows 要求 Python 3.12.4+ 私有 mkdir ACL；
部署者仍须审核父目录、账号和备份权限。数据库事件用数据库时间；断链时仅允许最近 30 秒
校准、≤2 秒误差的主机时间，检测时钟跳变。误差区间必须完全落在签名故障窗口内。

完整 journal 保留全部前导事件。导出器按固定 case 类型及故障时间窗口选择事件，完全位于
窗口外的事件仍留在源 journal；跨越窗口边界的时钟误差会拒绝，不修正成有利时间。

受保护签名流程须完成以下步骤（此仓库无生产私钥生成器或签名服务客户端）：

1. 独立数据库采集器签署 v3 before.json、after.json。独立故障方核对真实故障记录。
2. 使用类型化 `ProvenanceBinding` 构建 binding.json：version=1、challenge_digest、case_id、
   2–3 个 `(host_identity, sealed_journal_digest)`、真实独立 fault_record_digest。它只是本地
   provenance 元数据，不是新的签名版本。故障签名器必须核对每份私有日志、代码部署身份及
   实际故障记录，再把其规范 JSON 摘要放入 v3 Fault 的 protected_record_digest 并签名。
3. 在 binding.json 同目录放独立签署的 before.json / after.json / fault.json。导出只读本地
   文件并核对固定 trust SHA；不会联网签名或生成有效 Ed25519 签名。

```powershell
uv run python scripts/rc_host_witness.py export --config <config.json> --environment-id <UUID> --v1-report <v1.json> --plan <plan.json> --challenge <challenge.json> --state-dir <registry> --journal <sealed-host-dir> --binding <binding.json> --trust <trust.json> --trust-sha256 <独立固定SHA>
```

输出 `<sealed-host-dir>/unsigned-host.json`，退出 1、status NOT_VERIFIED。没有签名的草稿
不能解析为完整 SignedEvidence，也不能生产可信 PASS。受保护 Host 签名器核对构建/来源、
完整绑定、scope、Challenge 登记、故障窗口后，才添加登记身份并使用原
`Altas.RCCausalEvidence.v3\n` 域签名。独立 Reviewer 审核全证据集合后签字，最后仍由
`verify_rc_causal.py verify` 仅用于历史 v3 核查，不能作为正式最终 RC 放行入口，也不要提前消费
新 Challenge。正式保存证据审核采用下方 Custody 模式的严格入口。
私钥不进入 Worker 配置、仓库或常规日志；密钥轮换另行审批。签名证明来源，不单独证明物理事实。

原 v2 序列化/签名规则只读保持不变，不晋升为 v3。v3 A 现在明确要求无 DB 错误的 rejected
及相同租约证据；无法证明的旧 unavailable 收据不再满足 A。v3 状态检查允许原先无 Session、
零预算且无 Action/Attempt 的 Step 首次初始化 Session；已有身份仍不可替换，v2 行为不变。

## 独立离线 Provenance 预检

`scripts/rc_witness_provenance.py` 只读完整的两至三份 sealed journal、原 registry、
binding 和独立签署的 before/after/fault。它不连接数据库、不签名、不消费 Challenge，
不接受 `concurrent=true`。固定 trust SHA 与独立审核的 collector SHA 由受保护通道提供。

故障签名器须独立核对实际部署、进程 PID/nonce 与现场记录，然后在原 `ProvenanceBinding`
中增加 `process_headers`：每份完整 `CollectorHeader.model_dump(mode="json")` 的规范摘要。
该列表及全部 `(host_identity, sealed_journal_digest)` 一起被原 v3 Fault 的
`protected_record_digest` 保护。只复制 Header 或自行重算 Hash Chain 不会得到受保护签名。
空 `process_headers` 不改变旧 binding 的序列化摘要，旧导出继续可读，但不满足新门禁。
该绑定仍属于本地来源元数据，没有引入 v4 或改变 Ed25519 签名域。

```powershell
uv run python scripts/rc_witness_provenance.py --challenge <challenge.json> --state-dir <registry> --binding <binding.json> --trust <trust.json> --trust-sha256 <独立固定SHA> --collector-sha256 <独立审核的采集器SHA> --journal <host1完整日志目录> --journal <host2完整日志目录>
```

输出确定性的脱敏 unsigned report（含 report_digest），不写回 journal。退出 1 表示
`eligible_for_signing=true`，只供签名器继续审核；退出 2 表示拒绝。所有输出的发布状态均为
`NOT_VERIFIED`，不得将退出 1 或本地报告当成 RC 放行。缺失签名器、来源绑定、完整日志、
第二宿主、有效 Challenge、数据库对应关系或故障窗口时均不能进入签署准备状态。

实际 claim 记录 invocation start/completion/outcome 和随机调用身份，绑定 journal 的
Host/Process/Challenge/Batch 与观察的 Run/Step/Lease。在真实 DB 时钟查询两侧使用
`perf_counter()` 校准，保存往返与漂移误差上界；最近校准最多 30 秒，最多 2 秒误差。
当前漂移预算假设为每秒 1ms，跨校准不一致、跳变或不确定窗口均不能提供并发证明。
连接失败保留 `database_error`，无目标有效租约的空查询保留 unavailable，不能充作拒绝。

| Case A 观察 | 新独立门禁 | 原 v3 的含义 |
| --- | --- | --- |
| 第一宿主提交后，第二宿主才开始，仍在 competition_seconds 内 | SEQUENTIAL_VALID_LEASE，拒绝签署准备 | 可能 PASS，只证明有效 Lease 已存在 |
| 同 Run/Step/Lease，两个调用在计入误差后仍有必然重叠 | OVERLAPPING_INVOCATIONS，可继续来源审核 | 时间邻近本身不能作出这个区分 |
| 区间可能重叠但误差不能排除顺序 | TIMING_UNCERTAIN，NOT_VERIFIED | 不补造并发成功 |
| 只有 DB 不可用、无有效 Lease 的空查询或缺失宿主事件 | NO_CONTENTION_PROOF，NOT_VERIFIED | 不充作真实竞争拒绝 |

调用区间重叠仍不足以证明 SQL 锁竞争：进程可能在实际 SQL 前后暂停。v3 Host Operation
没有完整调用区间，故新报告始终另列 `transaction_contention=NOT_VERIFIED`，并要求独立
Reviewer 审核完整 fault-bound journal；不通过更改 v3 旧判定制造 PASS。本机签名 fixture、
Hash Chain、软件 Host Identity 与 PID/nonce 均不能独立认证物理宿主，报告始终列
`physical_hosts=NOT_VERIFIED`。原 Reviewer 签名与单次消费语义保留，正式 RC 由严格入口统一执行。

## 正式 RC 必须进入 Strict Release Gate

`rc_witness_provenance.py` 是签署准备检查，不能单独完成 RC 审核；旧 v3 PASS 也不能绕过它。
正式入口改用 [Strict Release Evidence Gate v1](rc-independent-acceptance.md#strict-release-evidence-gate-v1)
的 `verify_rc_release.py custody-precheck/custody-verify`，保留旧 v2/v3 和 Strict v1 命令作历史验证。

冻结完整 A–J Journal，保留所有前导事件与 seal。独立签名器核对真实进程与部署，签署包含
全部 journal/process header 摘要的 Fault Binding；Host 签名 payload 和 envelope 必须等于
从对应 Journal 确定性派生的草稿。只换同身份收据、重签时间/目标或复制 Header 都不替代检查。
F 等 Case 的 claim 前导可能已合法释放 Lease，不能把它充作仍持有 Lease 的证明；A/C 导出
的 claim 继续核对最终租约，原 Core 授权与采集边界不变。

先在操作者控制的私有 store 做 Prepared Freeze，并导出 UNSIGNED 签名交接请求。独立签名方核对
原 v3 准确待签 bytes、来源摘要和现场材料；Reviewer 可在非 Reviewer 返回后另做 Prepared Freeze。
最终收齐全部签名后做 Final Freeze。再 custody-precheck，最后 custody-verify 验证同一冻结副本并
消费原 registry，报告绑定 Manifest/完整字节摘要；原始可变路径不再作为最终验收对象。
保管目录所有者/管理员仍受信任，Hash 不能认证物理来源；复制或清空登记不能在同一受保护
store 内重用 Challenge。报告和缓存 JSON 均不是验证输入。命令参数、
退出 0/1/2、失败恢复与消费限制见上方手册；不要用历史 verify 预先消费同一 Challenge。
本机临时 PG 和合成签名只能证明软件契约，真实双宿主、SQL 锁竞争、独立签名来源、Provider、
Token 与生产放行仍 NOT_VERIFIED；真实收集与故障操作继续要求单独授权。

## 本轮验收范围

本地固定入口：`uv run python scripts/rc_host_witness.py local --bin D:/PSQL/bin`。
它只创建临时 PG，运行真实 Store/Worker 集成与受控子进程退出；签名测试使用合成密钥及合成
基础设施/故障声明，不是物理宿主证书。完整执行数字和同 SHA CI 在 RC checklist / 交付报告记录。

Python 3.12 专项：`uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --witness-only`。
Python 3.13 全套仍使用 `--release-gates`，包含新增的独立连接、Barrier/事务同步回归。
所有跳过原因通过 `-ra` 单独输出；SQLite 仅供离线回归，不能替代 PostgreSQL 验收。

真实双宿主 A–J、受保护签名服务接入、目标数据库断链演练、DeepSeek、LIVE TOKEN
CONFORMANCE、HA/PITR、生产放行：**NOT VERIFIED**。没有外部环境或付费授权，没有自动
远端连接/故障或付费请求。软件门禁通过只能支持进入下一次经批准的 staging 验收。
