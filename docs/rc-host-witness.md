# RC Host Witness Collection v1

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

Store.claim 是全局领取，未增加第二调度器。入口拒绝批次外尚活跃的 Run，但这不是 RLS，
不能阻止检查后其他 Writer 新增 Run；演练必须排除其他 Writer，并由操作者保证专库隔离。
两个宿主必须实际尝试同一目标才能证明 A；领取了不同 Step 不可拼接。

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
`verify_rc_causal.py verify` 使用固定信任根及单次 Challenge 消费登记接纳保存证据。
私钥不进入 Worker 配置、仓库或常规日志；密钥轮换另行审批。签名证明来源，不单独证明物理事实。

原 v2 序列化/签名规则只读保持不变，不晋升为 v3。v3 A 现在明确要求无 DB 错误的 rejected
及相同租约证据；无法证明的旧 unavailable 收据不再满足 A。v3 状态检查允许原先无 Session、
零预算且无 Action/Attempt 的 Step 首次初始化 Session；已有身份仍不可替换，v2 行为不变。

## 本轮验收范围

本地固定入口：`uv run python scripts/rc_host_witness.py local --bin D:/PSQL/bin`。
它只创建临时 PG，运行真实 Store/Worker 集成与受控子进程退出；签名测试使用合成密钥及合成
基础设施/故障声明，不是物理宿主证书。完整执行数字和同 SHA CI 在 RC checklist / 交付报告记录。

真实双宿主 A–J、受保护签名服务接入、目标数据库断链演练、DeepSeek、LIVE TOKEN
CONFORMANCE、HA/PITR、生产放行：**NOT VERIFIED**。没有外部环境或付费授权，没有自动
远端连接/故障或付费请求。软件门禁通过只能支持进入下一次经批准的 staging 验收。
