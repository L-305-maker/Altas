# V1.0 验证记录

日期：2026-10-05。发布候选提交在 GitHub Actions 的 Ubuntu 24.04 环境验证，
覆盖 Python 3.12/3.13、Node.js 24、PostgreSQL、Docker sandbox 与完整 Compose 栈。
模型相关检查仍使用离线 mock，没有使用真实 DeepSeek 或其他模型 API。

| 检查 | 实测结果 |
|---|---|
| 完整 pytest | 85 passed，6 skipped，1 条上游 Starlette/HTTPX 弃用警告；Python 3.12/3.13 均通过 |
| 临时真实 PostgreSQL | 4 passed；并发 workflow 注册、并发领取、旧 token 拒收、迁移升级/回退/模型一致性 |
| 官方 MCP SDK stdio | 真子进程启动、工具发现与 fingerprint 调用通过 |
| Docker sandbox | CI 构建真实镜像并执行非 root 运行与强制 timeout 测试，通过 |
| 离线业务评测 | 3/3，traceability_rate 1.0；不代表模型推理或医学质量 |
| Python 发布包 | agentflow-1.0.0 wheel 与源码 tar.gz 构建成功 |
| Next.js 生产构建 | 通过，含 TypeScript 校验 |
| 前端生产依赖 npm audit | 0 vulnerabilities |
| Docker Compose 配置 | docker compose config --quiet 通过 |
| 完整 Compose 业务冒烟 | PostgreSQL → migration → API/worker → Web 全部启动；经 Web 同源代理完成创建、审批、发布，最终 run succeeded 且引文可追溯 |
| Ruff | 代码检查与格式检查通过 |
| SQLite 100 步链基准 | 约 1.016 秒，98.5 步/秒；仅为此前本机微基准 |
| Playwright 浏览器人工验收 | 创建草稿、等待审批、审批通过、保存版本；桌面与 390px 移动布局通过，最终控制台 0 errors / 0 warnings |

常规 pytest 的 6 个 skip 是 4 个需要独立 PostgreSQL 的测试和 2 个显式 Docker
专项测试；它们均由独立 CI job / 临时数据库脚本实际执行，不把 skip 当成通过。

完整 Compose 冒烟测试使用独立临时 volume 和 mock provider，验证发布拓扑本身：
数据库健康后执行 Alembic，再启动 API、worker 和 Next.js；验证脚本等待 Web 层就绪，
通过 Authorization header 创建证据任务，等待持久审批，批准后确认 artifact 状态为
approved 且每条 quote 都能回溯到提交原文。测试完成后删除容器、网络和数据 volume。

当前仍未执行真实 DeepSeek 联调；这属于目标账户、配额、网络与模型行为的环境验收，
不能由 MockTransport 或 mock provider 替代。Playwright 浏览器检查目前是人工验收记录，
不是自动 CI gate；CI 的前端门禁为生产构建、依赖审计和经 Next.js 同源代理的业务冒烟。

FastAPI/Starlette 测试客户端当前有一条上游 HTTPX 弃用警告，不影响断言；未为了
隐藏警告而屏蔽测试输出。

上述验证证明 V1.0 的单租户内部部署基线可复现，但不证明任意生产负载或故障下都正确。
正式部署仍需依据目标环境验证 TLS、访问控制、限流、数据库备份/恢复、监控告警、
容量规划以及真实模型行为。

## 2026-10-05 Framework / Application 边界重构验证

- `uv sync --frozen`：通过。
- Ruff check / format check：通过。
- `uv run pytest -q`：90 passed、6 skipped（4 PostgreSQL、2 Docker opt-in）。
- `verify_postgres.py --bin D:/PSQL/bin`：专用临时实例，4 passed。
- Architecture boundary AST guard：3 passed，纳入 CI；Core 空业务 registry 与
  独立 research handler 注入测试通过。
- Living Guideline reference application API：证据、草稿、审批、发布制品、取消、
  SSE / WebSocket 回放保持通过。
- `evals/run.py`：3/3；评测实现和数据属于 `applications.living_guideline`。
- `uv build`：sdist / wheel 通过；`scripts/verify_wheel.py` 在 checkout 之外的
  临时虚拟环境安装 wheel，完整业务流程与打包的评测数据均验证通过，纳入 CI。
- hello_workflow 与 simple_agent 示例：通过。
- `npm ci --no-audit --no-fund`、`npm run build`：通过。
- `npm audit --omit=dev --audit-level=high`：0 vulnerabilities。
- 本机 Docker CLI 已安装，但启动 Docker Desktop 后 Linux daemon 仍不可连接；
  Docker sandbox 与 Compose E2E 未在本机运行，不能以此声称容器验收通过。
  CI 保留两个独立容器 job，Compose 现在明确启动 Living Guideline host。

未调用真实 DeepSeek API。上述验证不改变已有持久执行语义或业务质量声明。
