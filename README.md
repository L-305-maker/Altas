# AgentFlow 1.0

一个用于学习和单租户内部部署的持久工作流引擎：DAG、PostgreSQL 队列、worker
租约恢复、Agent 工具循环、人工审批，以及 Next.js 证据工作台。

完整业务路径：导入本地 `.txt` / `.md` 证据 → 提取可追溯引文 → 生成证据草稿 →
人工审批 → 保存不可变 JSON 版本。默认使用明确标记的离线 mock，不调用真实模型。

## 快速运行

要求 Python 3.12+（容器使用 3.13）、uv、Node.js 24。PostgreSQL 是部署数据库；
SQLite 是本机学习模式，不替代多 worker 的 PostgreSQL 验证。

```powershell
uv sync --frozen
uv run agentflow init-db
```

在启动 API 前，通过环境变量配置 `AGENTFLOW_API_TOKEN`，至少 16 个字符。
不要把凭据写进源码、提交到 Git 或发送到聊天。API 和工作台需要使用相同 token。
默认数据库为当前目录下的 `agentflow.db`，已忽略提交。

在三个终端分别启动：

```powershell
# 终端 1：API；先在这个终端配置 AGENTFLOW_API_TOKEN
uv run agentflow api

# 终端 2：worker；使用相同的工作目录和数据库配置
uv run agentflow worker

# 终端 3：界面
cd web
$env:NEXT_TELEMETRY_DISABLED = '1'
npm ci --no-audit --no-fund
npm run dev
```

访问 `http://127.0.0.1:3000`，连接工作台并提交证据。API 文档位于
`http://127.0.0.1:8000/docs`，业务路由要求 Bearer token。创建运行只负责持久化，
需要 worker 在线才能继续执行。输入校验失败不会产生可执行步骤。

## 服务部署

通过环境配置 `AGENTFLOW_POSTGRES_PASSWORD`、`AGENTFLOW_DATABASE_URL`、
`AGENTFLOW_API_TOKEN`。Compose 中数据库主机名是 `postgres`，数据库和用户名均为
`agentflow`，驱动为 `postgresql+psycopg`；URL 中密码须正确编码。可通过
`AGENTFLOW_CONCURRENCY`、`AGENTFLOW_LEASE_SECONDS`、`AGENTFLOW_POLL_SECONDS`
调整 worker 基线参数。

```powershell
docker compose up --build -d
```

Compose 先等待数据库就绪，再运行 Alembic，成功后启动 API、worker 和界面。
对现有数据库执行 `uv run alembic upgrade head`，不要用 `init-db` 替代版本迁移。
容器端口只绑定本机。对外开放时应由你部署的 HTTPS 反向代理负责 TLS、访问控制
和请求频率限制。本版是单租户 operator 权限，不提供用户注册或租户隔离。

## 模型与工具

- 默认 `AGENTFLOW_PROVIDER=mock`，不会访问模型 API。
- 后续配置 `AGENTFLOW_PROVIDER=deepseek`、`AGENTFLOW_DEEPSEEK_API_KEY` 和
  `AGENTFLOW_MODEL` 后即可使用真实 provider。模型名称应以你账户可用型号为准。
- DeepSeek 适配已通过 HTTP MockTransport 契约测试；真实 API 质量、配额和延迟
  需要提供环境配置后另行验证。
- `AGENTFLOW_TOOL_ROOT` 可开启受限目录的只读文本工具。
- MCP：配置 `AGENTFLOW_MCP_COMMAND`、JSON 数组格式的 `AGENTFLOW_MCP_ARGS`、
  `AGENTFLOW_MCP_TOOLS`。仅部署者决定命令与允许的工具；外部工具默认需要审批。
- Docker 沙箱默认关闭。先构建 `docker build -f deploy/docker/sandbox.Dockerfile
  -t agentflow-sandbox:1.0 .`，再在有受控 Docker daemon 的 worker 环境配置
  `AGENTFLOW_SANDBOX_ENABLED=true`。不向 API 或网页容器挂载 Docker socket。

## 验证

```powershell
uv run pytest -q
uv run ruff check src tests scripts migrations evals
uv run ruff format --check src tests scripts migrations evals
uv run python scripts/verify_postgres.py --bin D:/PSQL/bin
uv run python evals/run.py
uv run python scripts/benchmark.py
uv build
docker compose config --quiet
cd web
npm run build
npm audit --omit=dev --audit-level=high
```

常规测试会明确跳过需独立 PostgreSQL 或 Docker 的测试。PostgreSQL 脚本会建立
并关闭专用临时实例，不接触既有数据库。也可用 `AGENTFLOW_TEST_POSTGRES_URL`
指定**可清空的专用测试库**。Docker 测试需显式配置 `AGENTFLOW_TEST_DOCKER=1`。
CI 还会启动 PostgreSQL → Alembic → API → worker → Next.js 的完整 Compose 栈，
并通过 `scripts/verify_compose.py` 从 Web 同源代理执行“创建草稿 → 等待审批 →
批准 → 保存制品”的发布冒烟测试。

## 学习导航

- [逐层阅读代码](docs/learning-guide.md)：读什么、为什么这么设计、如何修改。
- [持久执行语义](docs/durability.md)：事务、租约、去重、崩溃恢复。
- [版本验收与边界](docs/version.md)：各阶段实现和验证方式。
- [运行、安全和观测](docs/operations.md)：配置、监控、备份与发布。
- [验证记录](docs/verification.md)：已实际运行的检查与尚未联调的部分。

本版会保存输入证据及步骤输出，请使用符合你数据管理要求的数据库和备份策略。
引文匹配只保证来源一致，草稿仍需人工判断其质量和适用性。
