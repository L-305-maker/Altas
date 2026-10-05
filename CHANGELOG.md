# Changelog

## 1.0.0

AgentFlow V1.0 是面向学习和单租户内部部署的 Production Baseline。

### Added

- 声明式 DAG 与同步/异步本地执行模型。
- PostgreSQL durable store、Alembic migration、数据库队列和多 worker 领取。
- lease、heartbeat、retry/backoff、cancellation 与 stale-result fencing。
- 有界 Agent runtime、mock/DeepSeek provider、ToolRuntime 权限与 schema 校验。
- MCP stdio 集成、只读文件工具与资源受限 Docker sandbox。
- FastAPI operator API、SSE/WebSocket 事件、持久人工审批和 artifact。
- Next.js evidence workbench。
- Prometheus/OpenTelemetry 基线、离线 eval、PostgreSQL/Docker/chaos 测试。
- Docker Compose 全栈发布冒烟验证。

### Release boundaries

- 单租户 operator 模型；不提供用户注册、多租户隔离或细粒度 RBAC。
- 外部工具副作用是 at-least-once 语义，不承诺 exactly-once。
- 证据输入支持 UTF-8 文本/Markdown；不包含 PDF/OCR 或在线文献检索。
- 真实模型质量、配额和延迟需要在目标账户与网络环境单独验收。
- TLS、公网访问控制、限流、数据库备份加密与保留策略由部署环境提供。
