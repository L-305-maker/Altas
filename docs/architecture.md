# AgentFlow V1.0 架构

本文件描述 V1.0 实际提交的代码，而不是长期目标目录。V1.0 是单租户内部部署与
学习基线：优先保证持久执行语义、可审计的人机审批和可复现部署，不为未来拆分预放空壳。

## 运行时拓扑

~~~text
Browser
  |
  v
Next.js workbench / same-origin proxy
  |
  v
FastAPI API ---------> PostgreSQL
                         ^      |
                         |      v
                     Worker <--- durable steps / leases / events
                       |
             +---------+----------+
             |                    |
        Model provider       approved tools
        mock / DeepSeek      filesystem / MCP / sandbox
~~~

API 只负责认证、输入校验和资源编排；实际步骤由独立 worker 执行。PostgreSQL 同时
保存 workflow 定义快照、run、step、租约、审批、事件和 artifact，从而避免队列与
状态数据库之间的双写。

## 代码边界

~~~text
src/agentflow/
├── __init__.py                         # 包版本
├── config.py                           # 仅环境变量配置
├── bootstrap.py                        # 组合根
├── domain/
│   ├── workflow/                       # DAG、ID、声明式 WorkflowSpec
│   └── execution/                      # 状态、依赖解析、本地运行对象
├── application/
│   ├── agents/runner.py                # 有界模型/工具循环
│   ├── orchestration/                  # 本地调度器、持久 worker
│   ├── ports/                          # Store / ModelProvider 协议
│   └── tools/runtime.py                # allowlist、schema、审批、超时
├── applications/living_guideline.py    # V1.0 真实业务纵切片
├── infrastructure/
│   ├── persistence/                    # SQLAlchemy 模型与 durable Store
│   ├── models/                         # mock / DeepSeek provider
│   ├── mcp/                            # 官方 MCP SDK stdio client/server
│   ├── sandbox/docker.py               # 无网络、限资源容器执行
│   ├── tools/filesystem.py             # 受限只读文本工具
│   └── telemetry/                      # Prometheus / OpenTelemetry
└── interfaces/
    ├── api/app.py                      # FastAPI、SSE、WebSocket
    └── cli/main.py                     # init-db / api / worker

web/
└── app/                                # Next.js 工作台与同源 API 代理

tests/
├── unit/                               # 纯规则与工具/agent 契约
├── integration/                        # durable engine、PostgreSQL、Docker
├── e2e/                                # API 业务闭环
└── chaos/                              # 崩溃、取消、过期结果

scripts/
├── verify_postgres.py                  # 临时 PostgreSQL 实例验证
├── verify_compose.py                   # 完整 Compose 发布冒烟
└── benchmark.py                        # 本地微基准
~~~

## 持久执行边界

Store 是 V1.0 的事务内核。一次领取只在短事务中取得 ready 或租约过期的 step；
用户 handler 在事务外执行。领取时生成 execution token，heartbeat 延长 lease，
complete/fail 必须同时满足 run 未终止、step 仍为 running、token 匹配且 lease
未过期。这样提供 at-least-once 执行和 stale-result fencing，但不承诺外部副作用
exactly-once。

PostgreSQL 使用行锁与 SKIP LOCKED 允许多 worker 竞争任务；SQLite 使用
BEGIN IMMEDIATE，仅用于本机学习和单进程测试。

## 人工审批与制品

requires_approval 的 step 在依赖满足后进入 waiting，并产生持久 ApprovalRow。
只有批准后才变成 ready；拒绝会使步骤失败并传播到 run。artifact 与 step 成功状态
在同一事务写入，并保存内容摘要。当前 living_guideline 流程是：

~~~text
本地文本/Markdown
  -> 精确子串引文
  -> 可审阅草稿
  -> 人工批准
  -> 不可变 JSON revision
~~~

## 安全边界

- API 是单租户 operator Bearer token，不是多租户 RBAC。
- 请求体、工作流规模、模型上下文、工具调用、工具输出均有显式上限。
- ToolRuntime 在单一入口执行 allowlist、JSON Schema、审批与 timeout。
- MCP 子进程默认不继承数据库密码和模型密钥。
- Docker sandbox 不联网、不挂载宿主目录、非 root、只读文件系统并限制 CPU/内存/PID。
- 事件和 trace 不保存输入正文、异常正文或凭据。
- 标准 Compose 不挂载 Docker socket；启用 sandbox 或特殊 MCP server 时应提供专用、
  受控的 worker 环境，而不是扩大 API/Web 容器权限。

## 部署与验证

docker-compose.yml 的基线拓扑为 PostgreSQL → migration → API/worker → Web；
Prometheus/Grafana 使用可选 profile。CI 同时验证 Python 3.12/3.13、Ruff、pytest、
临时 PostgreSQL、离线 eval、发布包构建、Next.js build/npm audit、Docker sandbox
以及完整 Compose 业务冒烟。

V1.0 的明确边界与实测证据分别见 docs/version.md 和 docs/verification.md。
