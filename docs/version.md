
| Version | 名称 | 核心问题 |
|---|---|---|
| V0.0 | Foundation | 项目骨架和工程规范 |
| V0.1 | Local Engine | DAG 到底怎么执行 |
| V0.2 | Persistent Engine | 状态如何持久化 |
| V0.3 | Worker System | 怎么异步/并发执行 |
| V0.4 | Durable Execution | 崩溃以后怎么恢复 |
| V0.5 | Agent Runtime | Agent 怎样运行在 Engine 上 |
| V0.6 | Tool / MCP / Sandbox | Agent 如何安全执行现实操作 |
| V0.7 | HITL + API + UI | 人如何参与长生命周期任务 |
| V0.8 | Observability + Evals | 怎么知道系统到底发生了什么 |
| V0.9 | Real Application | 用真实任务把架构打穿 |
| V1.0 | Production Baseline | 稳定、测试、性能、安全、文档 |

## V1.0 实现范围与验收

本版定位为单租户内部部署与学习基线。原始架构图描述长期目录方向，实际代码
以已实现的职责为准，不为每一个目录放空壳。V0.1 的本地 API 保留，服务模式
使用 Worker + Store。真实 DeepSeek 与 Docker 环境的实际联调另列，不以 mock
测试结果代替。

| 阶段 | 当前实现 | 可验证目标 |
|---|---|---|
| V0.1 | 同步/异步本地 DAG、失败传播 | 原有 51 个测试继续通过 |
| V0.2 | SQLAlchemy PostgreSQL/SQLite、Alembic、定义快照与检查点 | 重开连接后数据保留、迁移升级回退 |
| V0.3 | 数据库队列、并发 worker 槽位、行锁领取 | PostgreSQL 并发领取无重复 |
| V0.4 | 租约、心跳、令牌隔离、超时、退避、取消 | 真实子进程崩溃后恢复、旧结果拒收 |
| V0.5 | Provider 协议、DeepSeek 适配、离线 mock、有界 Agent 循环 | HTTP 契约及多轮工具测试 |
| V0.6 | 工具 schema/允许列表/审批、MCP stdio、只读文件、Docker 沙箱 | MCP 真子进程通过；容器执行需 Docker |
| V0.7 | 持久审批、认证 FastAPI、SSE/WebSocket、Next.js 工作台 | 浏览器创建—审批—保存版本通过 |
| V0.8 | 持久事件、可选 Prometheus 与本地 OpenTelemetry、评测 | 脱敏 trace 测试、离线评测通过 |
| V0.9 | 本地文本证据、来源摘要、引文验证、草稿、审批制品 | 全流程端到端测试与 3 个评测样例 |
| V1.0 | 锁文件、测试、CI、部署文件、输入限制、学习及运维文档 | 见 verification.md 的实测记录 |

证据输入当前支持 UTF-8 文本/Markdown，不包含 PDF/OCR、在线文献检索或临床质量
判定。每次审批发布产生独立版本，不自动执行语义合并。V1.0 不承诺外部副作用
exactly-once、多租户隔离或任意规模性能。

## v1.0.0 之后：Framework / Application Boundary Separation

本次只重构依赖边界，保留 1.0.0 包版本与持久执行语义，不增加产品功能。
Living Guideline 的 schema、workflow、handlers、router 和组合入口迁到
`src/applications/living_guideline/`；Core 不再注册业务内容。工作台和业务评测
属于 reference application。启动业务服务改用 `living-guideline api/worker`，
通用 `agentflow api/worker` 不包含 `/api/guidelines`。
旧 `agentflow.applications` 导入路径移除，不提供反向依赖兼容 shim。
