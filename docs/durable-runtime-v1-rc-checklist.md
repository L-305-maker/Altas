# Durable Runtime v1：Release Candidate 验收

## RC Evidence Verification & Independent Acceptance v2（2026-10-23）

审核基线 `9b70938d9fa8df9645aaea8d792c8dfeabadd352`，仅 main；没有新分支、PR、迁移、真实付费请求或外部故障演练。
新契约与操作者流程见 [RC Independent Acceptance v2](rc-independent-acceptance.md)。
独立数据库、部署、基础设施、宿主、故障与审核收据分别验证；v1 非预检 PASS 禁令保持不变。
软件门禁的实际执行结果如下；不能将下列 NOT_VERIFIED 当作代码 CI 的替代或失败跳过。

| 软件验证命令 | 实际结果 |
| --- | --- |
| `uv run pytest -q` | 1173 passed、504 skipped、212.76 秒；1 条已有 TestClient 弃用警告；skipped 不计通过 |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 506 passed、472 deselected、零跳过、268.61 秒；隔离临时 PG，包含新增只读采集、并发及历史迁移 |
| 最终采集/预检专项 | 12 passed、零跳过、12.44 秒；全新隔离 PG，实际 Worker 审批/UNKNOWN 与 Summary/Consumer |
| RC / Live / Staging 单元回归 | 78 passed、12.93 秒；合成独立密钥、MockTransport、CLI 防重放与付费禁用，不是真实宿主证明 |
| Ruff check / format --check | PASS，src/tests/scripts/migrations/evals，192 文件 |
| `uv build` / `scripts/verify_wheel.py` | PASS，仓库外安装 45 包；通用 Runtime、指标、Source→Summary→Consumer 与参考应用闭环 |
| 架构边界 / `evals/run.py` | 3 passed；离线 3/3、traceability_rate=1.0 |
| 三独立进程 Soak | PASS：`--seconds 180 --workload 6 --seed 7`；实际 196.39 秒、32 waves、200 Run、103 次工具派发、330 次领取；单机临时 PG，跨宿主 NOT_VERIFIED |
| 实际 pg_dump / pg_restore | PASS：0008、15 表、17 恢复 Run（4 个历史夹具）、已完成工具重派 0、新明确批准派发 1；Admission/Usage/Checkpoint v1–3/Summary/Consumer 保留；不是 HA/PITR |
| 本地 Docker / Compose | NOT_VERIFIED：DockerDesktopLinuxEngine 不可用；推送后实际 CI 结果另行核对，不能把跳过计为通过 |

| 验收范围 | 状态 | 证据边界 |
| --- | --- | --- |
| 真实双宿主 A–J | NOT_VERIFIED | 本次仅离线合成签名与临时数据库；生产资产清单、受保护宿主收据与独立审核尚未提供 |
| Live Provider | NOT_VERIFIED | 未授权付费；MockTransport 仅验证 v2 双层报告语义和单请求限制 |
| Live Token Conformance | NOT_VERIFIED | 无托管模型/完整 framing 证明；strict 继续 fail-closed |
| 生产发布 | NOT_VERIFIED | 离线验收报告始终不代表实时数据库核查或生产授权；发布负责人需独立确认目标环境与证据 |

## Production RC Staging & Evidence Gates v1（2026-10-22）

审核基线 `de1b29eddd4e7b396aa7144fa3c5ffb717b5abac`，仅 main，无分支/PR、付费请求或外部数据库操作。
完整操作者流程、A–J 检查来源与边界见 [Production RC Staging](production-rc-staging.md)。
仅修改 RC 脚本、测试和文档；`src/agentflow` 与历史 0001–0008 不变。

| 门禁 | 状态 | 本轮实际记录 / 边界 |
| --- | --- | --- |
| TDD 与只读预检 | PASS | 先复现缺失契约/旧文档、跨批次 plan 未拒绝、清理覆盖停止原因；离线门禁与 Live Mock 组合最终 40 passed；临时 PG 定向组合 23 passed。权限/环境标记/默认超时在真实隔离 PG 检查，TLS 为 false 时正确拒绝，未假装真实 staging |
| Schema / 证据 / 安全边界 | PASS | 双宿主手册 0005 → 实际 head 0008，版本不匹配不启动；冻结 v1 报告只为实际只读预检生成 PASS，含八项安全结果与校验 digest，不导入人为 passed=true。拒绝跨宿主/Provider/Token 伪 PASS；公开输出不含 URL、Token、原始正文或凭据 |
| PostgreSQL release gates | PASS | `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates`：502 passed、468 deselected、零跳过，265.03 秒；独立事务、租约/CAS、历史迁移、审批/UNKNOWN、Admission、Summary/Consumer、取消、旧身份及新最小权限/槽位检查 |
| 新增槽位清理回归 | PASS | 首次完整 PG 为 501 passed / 1 failed，真实发现池归还连接不释放 session advisory lock；显式解锁后完整重跑通过。未删断言、跳过失败或改变 Workflow 锁/租约语义 |
| 最终完整 pytest | PASS | `uv run pytest -q`：1131 passed、500 skipped、1 项已有 TestClient 弃用警告，229.33 秒。专用 PG、Docker 和显式付费的 skipped 不计通过；PG 已另行实际验证 |
| Ruff / 架构 / 离线评估 | PASS | check、format --check：186 files；架构 3 passed；离线评估 3/3、traceability 1.0 |
| 三 Worker 本机 Soak | PASS | `verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7`：192.42 秒、27 波、170 Run、88 派发、285 领取；混合审批/UNKNOWN、延期、取消、Summary/Consumer 与真实本机进程/数据库故障，cross_host=NOT VERIFIED |
| 实际逻辑备份恢复 | PASS | `verify_runtime_backup.py --bin D:/PSQL/bin`：head 0008、15 表逐项比较、17 个恢复 Run（4 个旧历史）；审批、v1/v2/v3 Checkpoint、v1/v2 Contract、Summary/Consumer、Admission/Usage 保留；已完成重派 0、新批准派发 1。不是 HA/PITR |
| 构建 / 仓库外 Wheel | PASS | `uv build`；`verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl`：新临时 venv 安装 45 包，Core、metrics、参考应用、摘要消费和离线评估通过。已有 hardlink-copy 与 TestClient 弃用警告保留 |
| 本地 Docker / Compose | NOT_VERIFIED | DockerDesktopLinuxEngine pipe 不存在；不将默认 Docker skip 计通过，最终交付报告核对新 main 的实际 GitHub CI |
| 双宿主网络故障 / 目标部署 TLS / 生产 HA-PITR | NOT_VERIFIED | 没有环境或独立授权。本机多进程、隔离 PG 重启和逻辑备份不能替代；提供只读预检、分离授权的有界合成命令、私有 plan 和 A–J 操作手册 |
| 真实 DeepSeek Smoke | NOT_VERIFIED | LIVE PROVIDER NOT VERIFIED。仅 Mock/MockTransport；显式付费双授权、单请求/无重试保护及安全进度报告测试通过，没有真实付费请求 |
| 真实托管模型 Token Conformance | NOT_VERIFIED | LIVE TOKEN CONFORMANCE NOT VERIFIED。实际生产 Profile 清单为空，DeepSeek strict 仍拒绝；离线向量或一次 usage 匹配不能证明 hosted framing 等价/普遍上界 |
| 新数据库迁移 | NOT_APPLICABLE | 本阶段没有 schema 变化；继续通过已有全部历史升级、回退与恢复测试 |

本表为推送前本地执行记录；最终交付报告提供新 main SHA 对应 GitHub Actions 链接和结果。
报告的 digest 不是签名/真实性认证，机器标识摘要不是物理宿主独立证明。
代码门禁通过可以支持下一步受控 RC 演练，不能据此宣称生产部署已放行；仍需 DBA 登记的独立 staging、
可信构建/镜像来源、实际 A–J 证据审查、真实 Provider 付费授权与独立 Token 证明。

## Verified Token Window Preflight v1（2026-10-22）

审核基线 `68fc2675932c579b83809465e4e6bd9be35978dd`，仅 main；
契约、官方来源及候选资产 SHA-256 见 [Token Window Conformance](token-window-conformance.md)。
有可信 Profile 的请求使用显式 v2，旧 v1 摘要及读取不变；仍为迁移 0008，无生产数据库操作。

| 检查 | 状态 | 证据/边界 |
| --- | --- | --- |
| 失败回归 | PASS | 先复现缺少 Profile 身份；另复现 Store 用 v1 已知计数预约绕过、版本布尔值被当作 1、冻结后并发修改导致计数/执行视图不同，再做最小修复 |
| 模型绑定、边界及完整请求 | PASS | Profile 模型/能力/资产/模板漂移；等于窗口允许、超一拒绝；Unicode、多工具/Schema、JSON；只计最终投影/Egress，生成器/消费者各自计数 |
| 旧事实与容量 | PASS | v1 固定 SHA、旧 Fixture；v2 复用 Attempt JSON；已有单事务/会话上限保留，取消 UNKNOWN 余量回归 |
| 最终完整 pytest | PASS | `uv run pytest -q`：1108 passed、497 skipped，244.56 秒；1 项已有 TestClient 弃用警告。跳过不计通过，专用 PostgreSQL 独立实际执行 |
| PostgreSQL 发布门禁 | PASS | `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates`：499 passed、467 deselected、零跳过，316.93 秒；全清单保留 489 个 PG 节点及 10 个共享用例，467 个 SQLite 副本交给完整 pytest |
| Ruff / 架构 / 离线评估 | PASS | check 与 format --check：182 files；架构 3 passed；离线评估 3/3，traceability 1.0 |
| 三独立 Worker 持续演练 | PASS | `verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7`：195.14 秒、32 波、200 Run、103 派发、329 领取；含合成 v2 Profile、旧审批/UNKNOWN/延期/取消和真实进程/数据库故障，cross_host=NOT VERIFIED |
| 实际备份恢复 | PASS | `verify_runtime_backup.py --bin D:/PSQL/bin`：0008、15 表、17 个恢复 Run（含 4 个旧终态）；v2 Profile、v1/v2/v3 Checkpoint、Summary/Consumer、Admission/Usage 完整恢复，已完成重派 0、新批准派发 1；不是 HA/PITR |
| 构建 / 仓库外 Wheel / 参考应用 | PASS | `uv build`；`verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` 在新虚拟环境安装，实际 Core/metrics/参考应用与 source→summary→consumer 闭环，离线评估 3/3；包含新 Profile/Conformance API |
| 本地 Docker / Compose | NOT VERIFIED | 本机 DockerDesktopLinuxEngine 不可用；两项 Sandbox 测试默认 opt-in 跳过。推送后核对最终提交的实际 Docker/Compose CI，不把本地跳过计为通过 |
| 真实 DeepSeek Counter | NOT VERIFIED | 当前目录/公开 V4 包不能证明托管 V4.1-Flash-1017/Pro-0813 的完整 framing 等价性，exact/upper_bound Profile 均未启用，strict 拒绝 |
| 真实付费 Token 对照 | NOT VERIFIED | LIVE TOKEN CONFORMANCE NOT VERIFIED，未授权、未发送付费请求；Mock Counter 只证明 Core 契约 |
| 真实 Tokenizer 加载/吞吐 | NOT APPLICABLE | 本轮没有启用生产 Tokenizer；已有请求/向量/资产大小界限，启动时一次验证，纯计数线程不阻塞事件循环，不承诺终止有缺陷同步线程 |
| 跨主机网络分区 | NOT VERIFIED | 隔离本机 PostgreSQL/多进程不能替代跨主机 staging |

本表记录推送前的实际本地结果；最终交付报告提供对应 main Commit 的实际 GitHub CI 链接。
代码契约可审核合入与真实 DeepSeek strict 可投产是两项不同结论。

原先 PostgreSQL 门禁重复执行 SQLite/PG 两个后端，在本机独立执行两次均触及原 600 秒上限，
这两次未计通过。现保留同一测试清单和 600 秒上限，专项去重 SQLite：收集比对确认原 966 项中
全部 PG 节点保留，所去除的仅为 467 个 SQLite 副本；选择器另有实际 pytest 回归。
完整 `uv run pytest -q` 仍执行 SQLite/全部共享测试，没有新增 skip 标记、关闭检查或放宽租约。

## Summary-Aware Context Consumption v1（2026-10-22）

审核基线 `9395dba35364c176ad0c9cb8a499b78864c04af7`；仅 main，没有新分支、PR、生产数据库或付费请求。
契约及部署边界见 [Summary Consumption](summary-consumption.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 先失败回归 | PASS | 基线缺少消费策略/工具；另先复现元数据携带模型自选 claim_id、UNKNOWN→FAILED 对账夹带任意结果、私有注册丢失 Runtime 授权及 Runner 校验、过滤声明后误用完整摘要覆盖率，再做最小修复 |
| 持久身份与授权 | PASS | 同 Run 明确依赖、真实 Step/Artifact/成功 Session/Attempt/Contract、原响应与完整来源；伪造依赖、跨 Run、缺 Attempt、摘要/引用/digest/Source 损坏、策略漂移预约前停止 |
| 角色与工具权限 | PASS | 元数据默认无生成正文/原声明标识；显式文本只出现在真实 tool 数据；普通工具 Egress、具体行动审批保留，视图与真实绑定进入请求身份 |
| 恢复与容量 | PASS | 消费模型/工具预约、读取结果、完成提交的真实子进程退出；确认丢失不重发，UNKNOWN 挂起，旧 token/CAS、业务取消和原始 CancelledError；真实 2 MB 单事务/8 MB 会话限额与取消余量 |
| 完整 pytest | PASS | 最终 `uv run pytest -q`：1056 passed、472 skipped，360.35 秒；1 项已有 TestClient 弃用警告，跳过不计通过；专用 PostgreSQL 另行实际执行 |
| 完整 PostgreSQL 发布门禁 | PASS | 最终 `verify_postgres.py --bin D:/PSQL/bin --release-gates`：917 passed、零跳过、420.68 秒；包括所有历史迁移/旧身份、审批/对账/延期、真实独立事务 CAS/fencing、消费者崩溃及取消回归 |
| Ruff | PASS | check 与 format --check，178 files already formatted |
| 三独立 Worker Soak | PASS | `--seconds 30 --workload 6 --seed 7`：41.12 秒、25 Run、15 派发、46 领取；含 source→summary→consumer，与审批/对账/延期/取消混合 |
| 逻辑备份恢复 | PASS | 实际 pg_dump/pg_restore：0008、15 表、16 Run（4 个旧 v1/v2 终态）；原始全部事实比较，摘要来源/Artifact/消费视图/请求契约可验证；已完成重派 0，新批准派发 1 |
| 构建 / 仓库外 Wheel / 参考应用 | PASS | uv build；安装后 source→summary→consumer 真闭环，原参考应用、Core、metrics；离线评估 3/3、traceability 1.0；架构边界 3 passed |
| 本地 Docker / Compose | NOT VERIFIED | 本机 Docker daemon 不可用；以最终提交的实际 GitHub CI 为准，不把本地跳过计为通过 |
| 真实 Provider / 跨宿主 / 生产长时负载及 HA/PITR | NOT VERIFIED | 真实付费请求 0；Mock、单机多进程及逻辑备份不证明真实 LLM 安全/质量、网络分区或零数据丢失 |

没有新增 Schema/迁移，0001–0008 保持；旧 Manifest/Contract 不补造消费身份，None 新字段省略。
并发运行本地验证时曾有一次 PostgreSQL 专项触及 600 秒门限，未计为通过；结束并发后，同一完整门禁
以原门限独占运行通过。没有放宽测试/租约/清理超时，也没有排除测试。
摘要生成和消费分别授权，选定引用覆盖率不是全文覆盖率，引用不是事实认证；保持非特权数据角色。
未发现已验证范围内未解决的 P0/P1。P2 边界包括生成内容幻觉/遗漏、公开分类并非 PII 检测、
哈希并非加密、完整来源反复验证的成本，以及字节上限不等于可信 Token Window/费用硬上限。
生产放行仍需独立 RC、真实 Provider 与跨主机验收；不得以本代码门禁代替。

## First-Class Durable Summary v1（2026-10-22）

审核基线 `a05f7f26e3af3e1b0449f2abf895d70652198ca9`，仅 main，无新分支/PR/迁移或生产数据操作。
完整契约、权限及限制见 [Durable Summarization](durable-summarization.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 先失败回归 | PASS | 基线没有 SummaryPolicy/Runner；实现过程中另外先复现并修复源 metadata 权限扩大、对账替换冻结读取结果（Provider.calls=1 → 0）、策略漂移阻断取消三项边界 |
| 独立执行与原始事实 | PASS | 显式 durable_summary Step；独立 Session/两轮 ModelAttempt/真实只读 Action/usage；源审批已消费、UNKNOWN 后人工确认、历史和 Artifact 不变；摘要失败只终结自身，不逆转源成功 |
| 来源与模型角色 | PASS | 同 Run 的真实已成功依赖、Step key→UUID、完整源历史/StepSpec/revision/hash；完整绑定授权、私密字段无原文/结果路径引用；六类合成恶意字符串只在真实 tool 数据内，不在 system/user，伪造引用/审批/动作 JSON 拒绝 |
| 确定性与 Artifact | PASS | 相同来源/策略/目标 Step 的 Manifest 固定；每条声明引用或不确定性、选定引用覆盖率/遗漏列表；事务内重验原响应/源/真实只读结果；恢复时只读复核成功 Attempt/Contract；不自动注入下游默认 Agent |
| 完整 pytest | PASS | `uv run pytest -q`：1008 passed、425 skipped，155.63 秒；1 项已有 TestClient 弃用警告；专用 PostgreSQL、本地 Docker 及显式付费用例的跳过不计为通过 |
| Ruff | PASS | `ruff check src tests scripts migrations evals` 与 `ruff format --check src tests scripts migrations evals`：171 files already formatted |
| PostgreSQL 发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：822 passed、零跳过，337.72 秒；独立引擎/CAS/旧 token、全部历史迁移、真实崩溃、取消和原有 Agent 回归 |
| 最后摘要/取消 PG 组合 | PASS | 独立临时 PostgreSQL 执行摘要契约、整体故障、真实子进程与完整取消回归：138 passed、零跳过，66.84 秒；包含发布专项收集后补充的取消策略漂移用例，计数重叠不合算唯一测试数 |
| 三 Worker Soak | PASS | `--seconds 30 --workload 6 --seed 7`：34.50 秒、24 Run、14 派发、43 领取；新摘要 Run 与原审批/对账/延期/取消并存 |
| 实际逻辑备份恢复 | PASS | pg_dump/pg_restore，0008、15 表、15 Run（4 个旧 v1/v2 终态）；Summary Source/Attempt/Contract/Artifact、原 v1/v2/v3、权限/审批/对账/用量全部事实一致；已完成重派 0，新批准派发 1 |
| 构建 / 仓库外 Wheel / 参考应用 | PASS | uv build；安装后实际 source Agent→durable_summary 闭环及类型化 Artifact 校验，原 Core/metrics/参考应用继续；离线评估 3/3，traceability 1.0，架构与通用 API 组合 5 passed |
| 本地 Docker / Compose | NOT VERIFIED | DockerDesktopLinuxEngine daemon pipe 不存在；以最终提交实际 GitHub CI 结果为准，不将本地跳过计为通过 |
| 真实 Provider / 跨宿主 / 生产 HA/PITR | NOT VERIFIED | 真实付费请求 0；Mock 不证明真实 LLM 抗注入/质量，单机进程和逻辑恢复不证明网络分区/零数据丢失 |

数据库专项另在隔离的真实 PostgreSQL 实际验证；不将其计数与完整 pytest 相加为唯一用例数。
保持 0001–0008，使用现有 JSON 和 Artifact 表，没有历史摘要/usage 回填；旧请求的新键 None 完全省略。
v1 仅行动公开摘录和状态元数据，4 KiB 来源输入；不摘要原 user/assistant 正文，不递归摘要，不接受 pins 或
workflow_identifiers。引用可定位但不认证事实，模型可遗漏或幻觉，覆盖率只代表选定引用。
确认可恢复模型失败才有界延期，终态失败/UNKNOWN 不重发；取消传播与最多五秒清理不变。
生产发布仍需真实 Provider、跨主机、长时负载和恢复运维验收。后续消费 Summary 必须独立授权并保持非特权角色，
不能授予行动、审批、对账或替换原历史的权限。

## Provider Context Egress v1（2026-10-21）

审核基线 `b0f4a7a031af2f8cab5df08ae3b6fdd50c68ce8b`，仅 main，无新分支/PR/迁移或生产数据库操作。
配置、兼容矩阵及残余外发路径见 [Provider Context Egress](provider-context-egress.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 先失败回归 | PASS | 原基线 full/recent 不识别显式 metadata_only、不能创建会话；新增测试先失败，实施后实际 Worker/MockProvider 接收过滤视图，数据库仍保存原合成私密结果 |
| 全模式披露与唯一策略 | PASS | full/recent 使用 v3 同一完整历史绑定决策；同名版本/摘要变化、缺绑定不继承公开授权；v3 策略完全相同才组合，metadata/public 冲突预约前拒绝 |
| 请求身份与历史 | PASS | 可选 egress/Contract 摘要原子绑定；v1/v2 真实旧夹具不变，v3 从审核基线只读源码单独捕获并逐字节重建；策略漂移不重试，UNKNOWN/确认丢失不重发 |
| 性能与容量 | PASS | 约 1.4 MB 合成结果、完整多工具批次与 UTF-8/Schema 硬边界；非 checkpoint 安全路径完整计量固定两次；清理余量与超大真实结果的 UNKNOWN 停止回归，不声称机器无关 SLO |
| 完整 pytest | PASS | `uv run pytest -q`：964 passed、393 skipped，208.36 秒；1 项已有 TestClient 弃用警告，跳过不计通过 |
| Ruff | PASS | check 与 format --check：164 files already formatted |
| PostgreSQL 发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：758 passed、零跳过，378.57 秒；真实独立事务唯一预约、旧 token/CAS、迁移、取消和进程崩溃等回归 |
| 最终新增/兼容性 PG 组合 | PASS | 独立临时 PostgreSQL 执行 Egress、v3 绑定、历史/取消及策略单元：102 passed、零跳过，32.62 秒；含发布专项收集后追加的大结果 UNKNOWN 用例，计数重叠，不合算唯一用例数 |
| 三独立 Worker Soak | PASS | `--seconds 30 --workload 6 --seed 7`：35.50 秒、23 Run、13 派发、41 领取；新负载为 metadata_only + 等价 v3，含 PostgreSQL 短暂中断与恢复 |
| 逻辑备份恢复 | PASS | 实际 pg_dump/pg_restore，0008、15 表、14 Run（4 个旧终态）；v1/v2/v3 与新 Egress 身份、历史绑定/授权/时间/Artifact 全事实比较；已完成重派 0，新批准派发 1 |
| 构建 / Wheel / 离线参考应用 | PASS | uv build、仓库外安装及 Core/参考应用/metrics；离线评估 3/3，traceability 1.0；架构边界 3 passed |
| 本地 Docker / Compose | NOT VERIFIED | DockerDesktopLinuxEngine daemon pipe 不存在；本次提交的实际 GitHub Actions 结果见最终交付报告，不将本地跳过计为通过 |
| 真实 Provider / 跨宿主 / 生产长时负载 | NOT VERIFIED | 付费请求 0；单机三进程/Mock 不能替代真实供应商或跨主机网络演练 |

393 skipped = 390 项专用 PostgreSQL + 2 项本地 Docker + 1 项显式付费测试；
数据库另由以上隔离专项实际验证。原有测试的固定 fixture.available_at 可能随日期到期，恢复夹具的测试辅助函数
仅把未选中 ready 场景隔离到未来，由用例显式置当前场景为到期；消息、Action、绑定、Snapshot、Attempt/Contract 不改写。

没有改动 Domain、历史迁移、Worker Scheduler、ToolRuntime 授权或 UNKNOWN 对账规则。
安全模式显式选择，未配置的旧 full/recent 仍完整回放工具结果；user/assistant/reasoning/arguments 仍有残余外发路径。
公开分类不是 PII/Secret 检测，ToolBinding 不是实现签名，字节不是精确 Token 或账单上限。
代码门禁不等于生产放行；Summary 的独立披露/派生身份/来源与不确定性/非特权承载前置条件见新文档，本轮未实现摘要。

## Evidence Disclosure v3 — ToolBinding 授权（2026-10-21）

审核基线 `918ae9215b299fa4dd936e9c96e01bf1141b8be1`，仅 main；无新表/迁移，0001–0008 不变。
契约与出处/角色矩阵见 [Evidence Checkpoint](evidence-checkpoint.md)，升级步骤见
[恢复手册](production-recovery-runbook.md)。公开授权、模型转述和 Action 执行权限分别核对。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 条件性 P1 失败复现 | PASS | 基线真实 Runner/Store 生成 read v1/v2 两次合法行动，工具名/路径相同但绑定不同；旧 v2 公开规则使重试 MockProvider 收到敏感合成结果，失败码 ToolVersionDisclosureNotAuthorized |
| 完整历史绑定与 Provider 消息 | PASS | 完整 ToolBinding 相等且 matches(action)，路径/public/mode/终态均核对；缺绑定、版本/摘要/Schema/description/timeout/副作用/审批漂移不披露原文；真实 Worker 多轮/审批/对账、元数据与 tool 角色配对通过 |
| v1/v2 原历史与 v3 身份 | PASS | v1 8219b93 夹具保留；v2 在修改生产代码前由 918ae92 捕获五类会话/九个 Attempt，旧 Manifest/Contract 逐字节身份可重建；v2 metadata 重试原身份继续，公开规则新预约拒绝，无补造绑定或升级 |
| 只读升级核查 | PASS | 按末次 Manifest 计三种版本，按持久 StepSpec 计公开策略/活跃未绑定/待重试阻断/人工处置；零尝试也核查，终态不重启，不输出正文、凭据、Action ID 或原契约 |
| 完整 pytest | PASS | `uv run pytest -q`：926 passed、371 skipped，136.29 秒；1 项已有 TestClient 弃用警告，跳过不计通过 |
| Ruff | PASS | check 与 format --check 完整通过，159 files already formatted |
| PostgreSQL 发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：717 passed、零跳过，314.51 秒；包含独立事务/CAS/fencing、迁移、真实崩溃、取消和审批/对账等原回归 |
| 最终绑定/兼容性 PostgreSQL 组合 | PASS | 新共享断言核对 v3 引用绑定摘要；单独临时 PG 执行绑定、checkpoint、升级核查与单元组合：107 passed、零跳过，29.67 秒 |
| 三独立 Worker Soak | PASS | `--seconds 30 --workload 6 --seed 7`：38.22 秒、23 Run、13 派发、41 领取；新工作负载使用 v3，未放宽 CAS、审批、UNKNOWN、租约或超时 |
| v1/v2/v3 逻辑备份恢复 | PASS | 真实 pg_dump/pg_restore，0008、15 表、14 Run（4 个真实旧 v1/v2 合成终态）；源与恢复库全部事实相等，旧身份/绑定/审批/用量保留，已完成工具重派 0，新批准派发 1 |
| 构建 / Wheel / 架构 / 离线参考应用 | PASS | uv build、仓库外 verify_wheel.py 安装及核心/参考应用验证；架构 3 passed，离线评估 3/3 |
| 本地 Docker / Compose | NOT VERIFIED | Docker Desktop daemon pipe 不存在；实际远端门禁结果以交付报告中本提交 CI 链接为准 |
| 真实 Provider / 跨宿主 / 生产长时负载 | NOT VERIFIED | 真实付费请求 0；Mock 消息契约不等于真实模型抗注入或事实正确性验收 |

371 skipped = 368 项专用 PostgreSQL + 2 项本地 Docker + 1 项显式付费测试。
数据库覆盖另以隔离 PostgreSQL 专项完成，Docker/Compose 以最终交付所指提交的实际 CI 为准。

v3 使用 checkpoint_version=3 / disclosure.policy_version=2，规则包含完整 binding；
来源实际 binding 摘要和匹配决定参与 Manifest/RequestContract，8 MB 配额和清理控制空间保留。
v1 新预约仍 AgentEvidenceV1ReadOnly；v2 公共规则新预约 AgentEvidenceV2PublicReadOnly，
不得更改旧 RunSpec/Binding 或把旧公开权限自动转成 v3。metadata_only 的 v2 没有原文输出分支，可原身份恢复。

本轮未发现其他可复现 P0/P1；已复现的按名称继承公开授权路径已阻断。
剩余边界：绑定不是实现签名，部署者必须在行为或敏感性改变时更新工具版本；public 不是 PII/Secret 检测；
摘录可能丢失语义，公开文本仍可误导模型。部署须停止旧 Writer 并统一升级，不能混用仍会公开旧结果的二进制。
以上代码门禁不等于生产环境放行。LLM-assisted Summary 仍需单独披露/执行授权、来源/不确定性、派生版本、
用量账本及非特权承载设计，本轮未实现或调用。

## 历史 Evidence Checkpoint v2 信任边界修复（2026-10-21）

以下为原验收记录；v2 广义公开规则当前只读，新的预约限制及 ToolBinding 核查以上节为准。

审核基线 `8219b9364ca2453dc27c733462d76e2e9c11012b`，仅 main，未新增迁移或修改 0001–0008。
信任角色矩阵、显式披露配置、固定来源和有限窗口算法见 [Evidence Checkpoint](evidence-checkpoint.md)。
默认 disabled 不变；旧配置仍解释为 v1，历史只读，新预约以 AgentEvidenceV1ReadOnly 阻断。
工具派生内容仅在真实 tool 配对内传输；unknown/sensitive 只有元数据，最近观察也执行同一披露规则。

| 检查 | 状态 | 实际证据 |
| --- | --- | --- |
| P1 失败复现与结构修复 | PASS | 基线测试实际出现 Provider.calls=1；修复后 v1 无预约/Permit/HTTP。v2 默认不派发模拟私密工具正文，原 system/user 内容不改写，没有假 Action/授权 |
| 披露与选择 | PASS | 精确 public 工具/字段白名单、失败/拒绝状态、中文/emoji/转义、UTF-8 边界；pins 成员优先但不重排同批 Action；缺 mandatory/摘要变化/字节不足安全失败 |
| 不变量与历史身份 | PASS | 基线真实 Runner/Store 合成夹具的三种会话、四个 Attempt 原 Manifest/Contract 可重建；v1 待重试只读阻断；v2 重试策略/Schema 漂移、CAS、旧 token、确认丢失、取消、审批/对账与容量预留回归 |
| 完整回归 | PASS | `uv run pytest -q`：901 passed、360 skipped，190.22 秒；1 项已有 TestClient 弃用警告，跳过不计通过 |
| PostgreSQL 发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：694 passed、零跳过，444.88 秒；CAS/fencing、迁移升降级、取消、审批/对账、真实进程崩溃、用量/准入/重试兼容；后补备份屏障独立事务用例见下一行 |
| Ruff | PASS | 完整 check 与 format --check，157 files already formatted |
| 备份边界独立事务回归 | PASS | 隔离真实 PostgreSQL 中，延期在停止 Writer 期间到期，Run 锁仍拒绝另一 Store.claim；解锁后可领取。信任边界/RC/单元组合 51 passed，零跳过 |
| 三独立 Worker Soak | PASS | `--seconds 30 --workload 6 --seed 7`：38.53 秒、17 Run、10 派发、32 领取；未重派 UNKNOWN/完成行动，未放宽锁和审批 |
| 混合 v1/v2 备份恢复 | PASS | pg_dump/pg_restore，0008、15 表、12 Run（含两个旧 v1 终态）；全部源/恢复事实比较，旧身份和终态保持，新批准派发 1，已完成工具重派 0 |
| 构建、仓库外安装、架构、参考应用 | PASS | uv build；verify_wheel.py 仓库外安装/核心/参考应用；架构 3 passed；离线评估 3/3 |
| 本地 Docker / Compose | NOT VERIFIED | DockerDesktopLinuxEngine pipe 不存在；不将本地跳过算通过。推送后实际 GitHub CI 链接/结果在交付报告中列出 |
| 真实 Provider / 跨主机 / 生产长时负载 | NOT VERIFIED | 真实付费请求 0；Mock 不证明真实模型抵御注入，单机三进程不证明跨主机分区行为 |

默认回归的 360 skipped = 357 项专用 PostgreSQL + 2 项本地 Docker + 1 项付费测试。
数据库覆盖另由隔离 PostgreSQL 发布专项和后补独立事务回归完成；本地 Docker 不可用，
实际 Docker/Compose 与 Python 3.12/3.13 GitHub CI 以最终交付链接所指提交为准。

复验曾发现两个演练夹具问题并修正：JSON 导入保留了显式事件 ID，却未推进 PostgreSQL 序列，
后续 claim 产生主键冲突；现在仅在测试加载器同步序列。备份脚本观察到短时准入延期后逐一关闭 Worker，
到期时另一个 Worker 仍可能合法领取；现在在持有 Run→Step 锁时停止所有 Writer，再捕获备份事实。
没有更改运行时租约、延长期限、降低断言或跳过失败测试。

代码可审核与生产环境放行分开判断。结构和披露规则不认证事实、不保证模型不误读数据；
公开字段授权者必须审查其完整内容。下一阶段生成式摘要需单独授权、来源/不确定性、派生版本、
账本、非特权承载和注入评估，不得改写旧请求身份或代替人工对账。本轮没有实施生成式摘要。

## 历史 Evidence-Anchored Context Checkpoint v1（2026-10-21）

以下为原验收记录；当时未覆盖工具摘录的 user 角色提升，不构成新的安全保证。
v1 当前仅允许只读验证，新执行按上节明确阻断。

审核基线 `6ee1c4b3bc4a8c5b077e31fdb00db4a63ab6ee8d`；仅 main 开发。
契约、算法、来源定位和权限边界见 [Evidence Checkpoint](evidence-checkpoint.md)。
复用既有 ContextProjectionManifest JSON，不新增迁移，不修改 0001–0008，不回填旧记录。

| 检查 | 状态 | 实际证据 |
| --- | --- | --- |
| 默认兼容及类型契约 | PASS | disabled 时无新 JSON 键；Policy/Reference/Manifest frozen；旧请求摘要与预算保持 |
| 来源与不可信数据 | PASS | 完整历史先验证；伪造来源/序号/摘录摘要即便重算引用摘要仍拒绝；仅首次 user goal；固定 StepSpec id/handler 来源受锁定定义核对；多工具、终态及对账结果准确配对 |
| 窗口与治理 | PASS | UTF-8/JSON 转义及 Schema 精确计量；完整回放超过 8500 字节时摘录+完整最近后缀仍可执行；证据或总量超限无 Attempt/Permit；Token/Usage/Admission 门控保留 |
| 请求及恢复身份 | PASS | 同逻辑失败重试 Manifest/Contract 不变；Schema 热更新拒绝；确认丢失不发 HTTP；真实子进程在预约、结果及延期重试边界退出后恢复 |
| 完整本地回归 | PASS | `uv run pytest -q`：866 passed、346 skipped，128.18 秒；1 项已有 TestClient 弃用警告 |
| Ruff | PASS | `uv run ruff check src tests scripts migrations evals`；`uv run ruff format --check src tests scripts migrations evals`，154 files already formatted |
| PostgreSQL 最终发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：668 passed、零跳过，328.00 秒；独立事务/CAS/旧 token、真实进程崩溃、取消/审批/对账及既有迁移回归 |
| 三独立 Worker Soak | PASS | `verify_durable_soak.py --bin D:/PSQL/bin --seconds 30 --workload 6 --seed 7`；最终复验 37.72 秒、23 Run、13 派发、41 领取；共享断言逐条核对 checkpoint 来源 |
| 真实逻辑备份恢复 | PASS | `verify_runtime_backup.py --bin D:/PSQL/bin`；0008、15 表、10 Run 全量事实保留（含 checkpoint/审批/对账/Usage/Permit/Artifact）；已完成工具重派 0，新批准派发 1 |
| 打包、架构与参考应用 | PASS | `uv build`；`verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` 仓库外安装；架构 3 passed；`evals/run.py` 离线 3/3 |
| 本地 Docker / Compose | NOT VERIFIED | Docker Desktop daemon pipe 不可用；2 项 Sandbox 跳过不计通过；本次提交的 GitHub CI 实际结果单列于交付记录 |
| 真实 Provider / 精确 Token / 跨宿主 / 长时生产负载 | NOT VERIFIED | 无付费调用，无新模型效果/事实准确性承诺；本轮单机短时多进程不替代跨宿主或长期生产验收 |

346 skipped 包括 343 项专用 PostgreSQL、2 项 Docker、1 项显式付费测试；不把跳过算作通过。
PG 由独立临时实例补齐，普通 CI 没有启用真实付费 Provider。

补充固定 StepSpec 来源时，一次并行 Soak 在停机前后整个 Checkpoint 比较失败。
该边界允许独立 peer 尚未完成并合法改变 Run 状态；现在先等待其持久成功再捕获停机快照，
仍严格比较全部字段，失败仅输出变化字段名。复验通过，没有放宽 CAS、租约、审批或超时。
新增可选来源不选择时的序列化和既有 v1 Manifest 摘要也由固定回归保护。

第二次 main CI（run 39588032129）在 Python 3.13 的既有
`test_worker_heartbeats_during_execution` 失败：120ms 租约、固定 200ms 睡眠后 Run 仍为 running。
该测试把共享 CI 的墙钟调度延迟当作续租保证；现在使用受控数据库时间并观察三次真实 Store 续租，
要求逻辑时间跨过原始租约期限后仍能完成，外层有 5 秒超时。没有延长租约、跳过测试或修改生产 Worker/Store。
修正后该用例独立重复 5/5 通过，相关回归 78 passed / 70 skipped，完整回归结果如上。
观测只收集完成前的前三次续租，后续正常心跳不再与终态提交竞争读取测试值。
最终提交的实际 GitHub CI 状态以交付链接为准，不以前一提交的绿色结果代替。

Manifest 只证明数据一致性，不认证现实事实；非特权摘录仍可能误导模型且丢失语义。
checkpoint 字节预算和 max_context_bytes 都不是精确 Token/费用上限，不提供外部 exactly-once 保证。
启用前升级所有 Worker；旧二进制不支持新模式/字段，停止 Writer、备份并处置启用会话后再考虑二进制回滚。
代码验证通过与目标环境生产放行分开判断。未来 LLM-assisted 摘要需独立授权、账本、来源和版本设计，本轮未实现。

## 结论与基线

PR #9 的原合并提交 `40862a93bc7db2a53bdcd4f320c367ed468db33a` 位于旧开发分支，并未将
RC 能力集成到 main。main 撤回提交 `3a25db0e295c21453892c1adff467436035b6055` 的代码树与
基准 `8fbb6e12b1ab3093ba9ae5f211da94b21aaac97e` 相同。本次仅在 main 应用这两个代码树之间
已审核的差异，以 `f26f192` 保存集成提交；其代码树与原 PR #9 合并树完全相同，再补三项安全修复。
没有重写历史、创建分支或 PR，也没有修改旧合并记录。本页原 PR9 证据为历史记录，本轮结果另列于下方；
后续部署必须重做环境项。

**代码可审核/合并不等于当前环境获准生产发布。** 建议进入隔离 RC staging 验收；真实 Provider、
跨主机和生产配置仍未验证，不能宣称 Production Ready。外部副作用不保证 exactly-once。

状态定义：PASS 实际执行通过；FAIL 实际执行失败；NOT VERIFIED 未具备环境/授权或尚未执行；
NOT APPLICABLE 只用于明确不适用的部署项并注明理由，不用来代替缺失证据。

## 验收矩阵

| 项目 | 状态 | 证据 / 发布条件 |
| --- | --- | --- |
| Domain、预算、顺序、终态 | PASS | 完整 pytest：606 passed、204 skipped；专用门控单列 |
| AgentStore CAS / fencing / Worker 取消 | PASS | 真实临时 PostgreSQL release-gates（结果见下） |
| Runner、审批、UNKNOWN 对账、重试、上下文 | PASS | 完整回归、真实 PG 及 soak 组合验证 |
| PostgreSQL 备份恢复 | PASS | 实际 pg_dump/pg_restore，12 表、7 Run 全量事实一致；已执行工具重派 0，待审批新批准派发 1 |
| 单机独立 Worker 稳定性 | PASS | 180 秒目标实际 194.97 秒，29 波、179 Run、91 Action、298 次领取；最终短样本 34.24 秒 |
| 跨主机部署 / 网络分区 | NOT VERIFIED | 无双宿主；见 recovery runbook 的 staging 入口 |
| 真实 DeepSeek API | NOT VERIFIED | 未提供专用付费授权/Key，本轮真实请求 0；默认 live 跳过 |
| 指标故障与只读核查 | PASS | COMMIT 后真进程退出、三进程共享目录/API、坏目录、本地重启与只读审计 |
| 数据库迁移和历史审计 | PASS | 0001–0003 不修改，临时 PG 迁移回归和恢复库 upgrade head |
| 发行包 / 安装 wheel | PASS | sdist/wheel 构建，仓库外安装、Durable/认证指标/参考应用成功 |
| Docker Sandbox / Compose | NOT VERIFIED | 本地 daemon 状态与本分支 CI 另行记录，不借用 Mock 结果 |
| 架构 / 参考应用 / 离线评估 | PASS | 架构 3 passed；参考应用回归及 offline 3/3 |
| 生产数据库连接 / 超时 / 租约 / 心跳 | NOT VERIFIED | 下表是验收条件，不是当前生产实测 |
| HA 切换 / PITR / 零 RPO | NOT APPLICABLE | 本轮只实现逻辑备份恢复门禁；需要这些 SLA 的部署须另行演练 |

## 实际执行记录

| 命令 | 本地实际结果 |
| --- | --- |
| `uv run pytest -q` | 606 passed、204 skipped，126.05 秒；1 项已有 Starlette TestClient 弃用警告 |
| `uv run ruff check src tests scripts migrations evals` | PASS |
| `uv run ruff format --check src tests scripts migrations evals` | 116 files already formatted |
| `uv run python scripts/verify_postgres.py --bin D:/PSQL/bin --release-gates` | 394 passed，零跳过，241.73 秒；真实临时 PG，含 SQLite 对照 |
| `uv run python scripts/verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7` | PASS；194.97 秒、29 波、179 Run、91 不同 Action 派发、298 次领取 |
| 同脚本 `--seconds 30 --workload 6 --seed 7` | PASS；34.24 秒、3 波、23 Run、13 Action、41 次领取；最终记录/权限/deadline 检查 |
| `uv run python scripts/verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；12 表、0003、7 Run；已完成 Action 重派 0，原待审批 Action 明确批准后新派发 1 |
| `uv build` | wheel + sdist 成功 |
| `uv run python scripts/verify_wheel.py dist/agentflow-1.0.0-py3-none-any.whl` | 仓库外安装、参考应用、Durable Step/Artifact、认证指标及离线评估成功 |
| `uv run pytest -q tests/unit/test_architecture_boundaries.py` | 3 passed |
| `uv run python evals/run.py` | offline 3/3 |
| `uv run pytest -q tests/integration/test_docker_sandbox.py` | 2 skipped，本地无 daemon，未计通过 |
| `docker info` / `docker compose config --quiet` | daemon pipe 不存在；Compose 缺部署环境变量，本地 E2E 未执行 |
| `uv run python scripts/verify_live_provider.py` | LIVE PROVIDER NOT VERIFIED，requests=0 |

最后增加损坏 Step 状态的脱敏失败回归，再改为只输出固定 invalid 分类。
同一审计文件在新建临时 PostgreSQL 上运行双后端 **6 passed、零跳过**，Ruff/格式检查再次通过。

204 skipped 包含专用 PG、Docker 和 1 项显式 live 门控，不计通过。真实 PostgreSQL 由独立临时实例
专项补充。本分支 GitHub CI 的实际结果在 PR 中记录；本地 Docker 不可用不借用上游 CI 的成功替代。
MockProvider/MockTransport 不是供应商实测。完整故障窗口复用 [v1 verification](durable-runtime-v1-verification.md)。

新增验证先执行失败回归：审计入口缺失、隔离资源入口缺失、live 双重授权及 Durable harness 缺失；
再实现入口。实际演练也发现测试断言遗漏“崩溃对账后真实有界 Step 重试”的 ready 状态，以及被取消
前尚未创建 Session 的 Task。仅修正测试检查器/验证器，不修改 Domain 或运行时状态规则。

## main 集成与安全修复验证

1. 干净基准完整回归：590 passed、196 skipped；恢复原 PR9 后完整回归：606 passed、204 skipped。
   先验证原始 30 秒 Soak、真实 7 Run/12 表备份恢复和 Ruff，再创建集成提交。
2. 安全回归先失败后实现：实际 Worker 日志含 PRIVATE Task 数据、环境变量独自启用 live、缺少分页。
   测试拦截付费脚本，失败也不能发送真实请求；失败输出只使用安全错误码。
3. JSONL 只留租约身份，保存在每 Worker 私有目录；原 token 保持不变，用真实 Store.claim 的 Task
   验证 load/commit_transition 均 LostLease。Action ID、幂等键和所有持久化不变量保持原检查。
4. 默认 pytest 即使有模拟付费授权与 Key 仍跳过；显式选项 + 授权 + Key 的启用路径只用 MockProvider
   回归。独立脚本仍需要 --allow-paid-request，手动 Provider Job 仍默认关闭。
5. 审计新增有界 offset/next_offset，每页最多 10000 Step、明确排序、只读、无原始身份输出。跨页是
   独立快照，更新时可能移动，不声称多页构成一次全库一致快照。

本轮本地实际结果见下表；main CI 在推送后独立核对，不把跳过项记为通过。

| 检查 | 本轮结果 |
| --- | --- |
| `uv run pytest -q tests/unit/test_rc_tools.py` | 15 passed |
| `uv run pytest -q tests/integration/test_runtime_audit.py` | 8 passed、8 skipped（PG 专项另测） |
| `uv run pytest -q tests/integration/test_runtime_metrics_loss.py` | 4 passed、4 skipped（PG 专项另测） |
| `uv run pytest -q tests/integration/test_rc_fencing.py` | 1 passed、1 skipped（PG 专项另测） |
| `uv run pytest -q`，模拟付费授权/Key 均存在、未给 live 选项 | 618 passed、210 skipped，真实付费请求 0 |
| Ruff check / format --check | PASS；117 files already formatted |
| `uv build` / `scripts/verify_wheel.py` | PASS；仓库外安装、Durable/认证指标/参考应用/离线评估 |
| 架构检查 / `evals/run.py` | 3 passed / offline 3/3 |
| `verify_postgres.py --bin D:/PSQL/bin --release-gates` | 406 passed、零跳过，164.36 秒；真实临时 PG，含 SQLite 对照 |
| `verify_durable_soak.py --bin D:/PSQL/bin --seconds 180 --workload 6 --seed 7` | PASS；190.47 秒、34 波、209 Run、106 Action 派发、346 次领取 |
| `verify_runtime_backup.py --bin D:/PSQL/bin` | PASS；0003、12 表、7 Run；已完成工具重派 0，明确新批准派发 1 |
| 本地 Docker Sandbox | 2 skipped；daemon pipe 不可用，不计通过 |
| 最新 main Docker/Compose/全部 CI | 推送后核对实际运行；最终交付报告提供链接，不借用旧分支结果 |

真实 DeepSeek 与跨主机仍为 NOT VERIFIED；本轮无新增 Core、迁移、Scheduler 或授权语义改动。

## 运维关注项

| 检查 | 部署验收条件 |
| --- | --- |
| PostgreSQL 身份和网络 | 专用最小权限角色、TLS/网络边界、Secret 不入日志，测试库与生产库隔离 |
| 连接池 | 按 API + 每个 Worker 的池上限核算总连接数，使用有限 connect_timeout；预留备份/运维连接 |
| statement_timeout / lock_timeout | 在 Worker 角色/连接配置有界超时，验证实际生效；演练使用 5s / 2s，不直接宣称适合所有生产负载 |
| 租约 / 心跳 | 心跳周期小于 lease（当前 Worker 为 lease/3），为数据库超时、调度延迟留余量，旧 token fencing 生效 |
| 取消清理 | 最多 5 秒协程等待；不能强制结束同步数据库线程，迟到提交依赖原 token/revision 校验 |
| 工具审批 | 工具实现变更更新版本；授权绑定版本、策略、参数 digest；已消耗审批不得复用 |
| 人工等待 | WAITING_APPROVAL / RECONCILIATION 配值班 SLA；等待久是提醒，UNKNOWN 不能自动重派 |
| 延期重试 | 数据库时钟 available_at、持久 continuation、相同逻辑模型 ID；模型预算与 Step 失败 attempts 独立 |
| 上下文 | UTF-8 序列化字节上限不是 token/费用限额，不截断协议或删除审计历史 |
| 指标 | 显式启用、认证、有限 labels；本地重启/提交确认丢失可少计；共享目录必须受保护且停部署后清理 |
| 持久核查 | SELECT-only 角色，限定 since/limit/超时，truncated 报告不是全库健康证明；退出 1/2 触发调查 |
| 备份 | 加密、权限/保留期、停止 Writer 顺序、隔离新库恢复、核对备份之后的外部副作用 |
| RC 放行 | 本版本 CI 通过 + 目标环境演练记录 + 人工发布授权；长测单独手动门禁 |

Prometheus 与数据库不是逐事件相等的双账本。审计仅查询当前持久事实，不重建全部历史、不修改业务数据。
有限 seed/时长无法穷举所有交错；真实跨主机、生产负载及真实 Provider 证据应附在部署验收记录。

## Context Projection v1 验收（2026-10-18）

基线 main 为 15d75c2f6dee1c92e784278f30b1b31ac557efa4，其 GitHub CI 已核实成功。
本次只新增确定性投影及逐尝试 Manifest，不修改 Action/Agent Domain、Scheduler、审批或对账规则。
详细配置、算法与迁移契约见 [Context Projection](context-projection.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 完整 pytest | PASS | 742 passed、294 skipped，114.75 秒；1 项已有 TestClient 弃用警告 |
| Ruff check / format --check | PASS | src tests scripts migrations evals；141 files already formatted |
| 新旧消息视图 | PASS | 合成 3 轮、每轮 2 个 Action，含 Schema：1766 → 686 UTF-8 字节，保留 1 轮、省略 2 轮 |
| 真实 Worker 与历史不变 | PASS | full 在 1500 字节限额停止；recent_complete_rounds 完成，原消息、结果、Action 与 Artifact 断言不变 |
| 重试 / UNKNOWN / COMMIT 确认丢失 | PASS | 同逻辑调用复用 Manifest；Schema 漂移、旧无 Manifest 重试保守停止；UNKNOWN 不重发 |
| 准入 / 观察用量 / 取消容量 | PASS | 投影不绕过 Permit 与软阈值；Manifest 计入会话限额，近容量取消仍可保存 UNKNOWN |
| SQLite 与 Alembic | PASS | 0007 升级、回退到 0006、再升级、check；原数据不变，旧 Manifest 为 None |
| 真实 PostgreSQL 完整发布门禁 | PASS | verify_postgres.py --bin D:/PSQL/bin --release-gates：566 passed、零跳过，230.29 秒；含独立事务 CAS、真实旧 token、取消、审批、对账及迁移 |
| 三独立 Worker Soak | PASS | 30 秒目标，实际 34.22 秒，seed=7，23 Run、13 Action、41 领取；单机隔离临时 PG |
| 真实 pg_dump / pg_restore | PASS | 0007、15 表、10 Run 全量事实一致（含 Manifest）；已完成工具重派 0，明确新批准派发 1 |
| 构建 / 仓库外 Wheel 安装 | PASS | sdist + wheel；投影配置、Durable Step、许可、用量、认证指标及参考应用 |
| 架构 / 离线评估 | PASS | 架构边界 3 passed；参考应用 offline 3/3 |
| 本地 Docker Sandbox / Compose | NOT VERIFIED | daemon pipe 不可用；Sandbox 2 skipped；最终 main CI 结果在交付记录中单列 |
| 付费 Provider / 跨主机网络分区 | NOT VERIFIED | 真实模型请求 0；无跨主机测试环境，不用 Mock 或单机 Soak 替代 |
| 生产环境部署放行 | NOT VERIFIED | 仍需目标环境连接/超时/租约、工具版本、外部副作用与人工运维验收 |

294 skipped 不计通过，主要是独立 PostgreSQL、Docker 和显式付费门控；PG 由专用临时实例补充。
投影专用 PG 用例进入原 verify_postgres.py 门禁，未新增或关闭 CI Job。

专项曾出现 564 passed / 1 failed：原三 Worker 测试把并发下合法的空 tick 当作失败。
Store.claim 使用 SKIP LOCKED，资格扫描与实际 Step 加锁之间也可丢失本次领取机会，正常 Worker 循环会继续轮询。
只将测试改为原 15 秒总超时内轮询，并额外断言三个不同 Run 恰好各被领取一次；仍要求 1 个 Permit/Attempt、
2 个持久延期。新增真实 PG 行锁空领取回归，与三 Worker 用例连续五轮均通过（共 10 次检查）。
不改 Scheduler、CAS、租约或准入额度，不跳过失败用例。

发布注意：旧 0006 的失败逻辑调用没有 Manifest，恢复重试会以 AgentContextProjectionChanged 终止，不能补造身份。
升级前应识别这些等待重试会话并制定运维处置；正常 upgrade/备份保留已有 Manifest，downgrade 0006 会移除它。
投影可能丢失早期语义，默认 full；字节上限不是供应商精确 Token 或费用上限；完整历史仍受 8 MB 存储额度约束。
代码验证通过不代表真实 Provider、跨主机、生产部署或外部副作用 exactly-once 已获证明。

## Immutable Model Request 验收（2026-10-20）

基线 main 为 347a5af158def299cb608d5bc18e8eba83467651，其 CI 已核实成功。
契约、能力、Token 计数保证等级和升级处置见 [Model Request Contract](model-request-contract.md)。

| 检查 | 状态 | 本轮实际证据 |
| --- | --- | --- |
| 完整 `uv run pytest -q` | PASS | 780 passed、320 skipped，125.39 秒；1 项已有 TestClient 弃用警告 |
| Ruff check / format --check | PASS | src tests scripts migrations evals；148 files already formatted |
| 请求身份与 HTTP 一致 | PASS | MockTransport 校验 model、max_tokens、endpoint 在预约后配置变更时仍使用冻结值；无付费请求 |
| 重试与 Session 固定配置 | PASS | 同逻辑调用 Contract/Manifest 不变；模型、Provider、输出或能力漂移在新 Attempt/Permit 前拒绝；新轮次不能静默更换 Session 配置 |
| 预检顺序与资源治理 | PASS | 字节、能力、可信计数窗口先于预算预约；配额/Observed Usage 仍生效；无可信计数的严格模式拒绝 |
| SQLite / 0008 / 只读升级核查 | PASS | 0008 → 0007 → 0008 / Alembic check；Action、审批、对账、Artifact 及旧事实不变；旧 Contract 保留 None；审计覆盖 0006/0007/0008 |
| PostgreSQL 完整发布专项 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：617 passed、零跳过，251.15 秒；含真实独立事务竞争及旧 token fencing |
| 三独立 Worker Soak | PASS | 30 秒目标，实际 34.75 秒，seed=7；23 Run、13 Action 派发、41 次领取；单机临时 PostgreSQL |
| 实际 pg_dump / pg_restore | PASS | 0008、15 表、10 Run 全量事实一致，含 Request Contract；已完成工具重派 0，明确新批准派发 1 |
| 构建 / Wheel / 架构 / 离线评估 | PASS | `uv build`；仓库外 Wheel 安装与 Durable Step；架构 3 passed；参考应用离线 3/3 |
| 本地 Docker / Compose | NOT VERIFIED | Docker daemon pipe 不可用；Sandbox 2 skipped，不计通过；推送后由本次 main CI 验证，实际链接见交付记录 |
| 真实 Provider / 真实 Token Counter / 跨宿主 | NOT VERIFIED | 未启用付费选项；无已验证 DeepSeek tokenizer 或跨宿主环境；合成 Counter 和单机 Soak 不替代真实验收 |

完整测试的 320 skipped 包括 317 项专用 PostgreSQL、2 项 Docker 和 1 项显式付费测试。
PostgreSQL 由上述独立临时实例补齐；未跳过失败检查或修改历史迁移。
新测试随原发布门禁运行，没有新增付费 CI 或第二套调度器。

旧迁移测试中“无请求身份仍可重试”的假设已更新为拒绝：用量计数和历史事实保留，预算不重置，
不能通过旧 Store 接口重新预约未知身份的失败调用。新夹具在合法完整工具映射上验证迁移，不放宽 ContextBuilder。
部署前需只读核查、停止 Writer、备份并处理旧待重试调用；0008 回退会删除契约，重新升级不能恢复已删身份。

Token 窗口保证仅限经过验证的 Counter / 上界；默认未知，不是费用硬上限。
供应商模型别名、实际权重或账单不由 Contract 证明；真实 Provider、跨宿主、长时生产负载和部署放行仍需独立验收。

## Provider Compatibility / Request Conformance v1.1（2026-10-20）

基线 main 为 0680fdc7358394dce4e1f0f21277400239387fa9。本轮不新增迁移，保留 0008 及全部历史请求字段。
官方 Chat、Models & Pricing、Change Log 和 Tokenizer 资料已在 [契约文档](model-request-contract.md) 记录查询日期、
支持名单、采样限制和保证边界；[恢复手册](production-recovery-runbook.md) 说明受控部署顺序。

| 检查 | 状态 | 实际证据 |
| --- | --- | --- |
| 新部署与生命周期 | PASS | Settings、Compose、示例环境均默认 deepseek-flash；mock 默认不变；两个规范名称允许，旧别名和未知名称在请求前拒绝 |
| 新旧接口 wire 合规 | PASS | 两个模型 × prepare/respond/complete；MockTransport 验证 HTTPS、认证 header、模型、输出、流式关闭、JSON 格式、采样、Schema 和工具 ID；不是付费 API 联调 |
| 冻结与错误分类 | PASS | 配置/上下文/Schema 热更新不改变已准备 payload；429、4xx、5xx、连接/读取超时、坏 JSON/响应单次请求并脱敏 |
| 已复现的请求选项缺口 | PASS | 先复现 context_json 夹带 temperature/thinking 与空 tools 的不同 wire，再拒绝契约外顶层键并规范空 Schema；合法 v1 摘要保持不变 |
| 旧 Session 与部署切换 | PASS | 真正 Worker/Store、历史 v1 夹具；更换新默认不改写或重试旧别名调用；当前规范模型仍以同一契约/Manifest/逻辑 ID 重试；保留非空 Usage、Permit、Action 和 Artifact |
| 只读生命周期核查 | PASS | version=2 有界聚合；旧/未知模型、旧待重试、执行中、缺身份、终态逻辑调用、去重处置数；0006/0007/0008 不写入、不输出持久身份 |
| 完整 pytest | PASS | `uv run pytest -q`：831 passed、324 skipped，129.28 秒；1 项已有 TestClient 弃用警告 |
| 真实 PostgreSQL / 历史迁移 | PASS | `verify_postgres.py --bin D:/PSQL/bin --release-gates`：625 passed、零跳过；含独立事务、CAS、fencing、取消、审批/对账及既有 Alembic 回归 |
| 静态 / 架构 / 构建 | PASS | Ruff check、format --check（150 文件）、架构 3 passed、uv build、隔离 Wheel 安装、参考应用离线 3/3 |
| 三 Worker Soak | PASS | 30 秒目标、实际 36.33 秒；seed=7、17 Run、10 Action 派发、32 次领取；单机临时 PostgreSQL |
| 逻辑备份恢复 | PASS | 0008、15 表、10 Run 全事实一致，保留 Contract/Manifest/旧审计；已完成工具重派 0，新批准派发 1 |
| 本地 Docker / Compose | NOT VERIFIED | daemon pipe 不可用；普通 pytest 的 2 项 Sandbox 跳过不计通过；本次 main CI 的实际结果由交付链接单列 |
| 真实 Provider / Token Window / 跨宿主 / 生产放行 | NOT VERIFIED | LIVE PROVIDER NOT VERIFIED；没有付费授权或目标部署演练，本轮没有真实模型请求或已验证的 DeepSeek Counter |

324 skipped 包含 321 项专用 PostgreSQL、2 项 Docker 和 1 项显式付费测试；PG 另由上述临时实例补齐。
不把跳过计为通过。旧分类测试的虚构模型名改为规范模型后仍检查相同错误，不绕过白名单或关闭检查。
本轮只对文档开展网络核实；真实 Smoke 保留显式付费选项、环境授权、专用 Key、一次请求与原超时。

严格 Token 窗口仍会在无可信计数器时拒绝；官方存在离线资源不代表已验证本项目完整请求模板。
请求契约不证明供应商权重、默认 thinking 行为、采样实际生效、真实费用上限或 exactly-once 账单。
