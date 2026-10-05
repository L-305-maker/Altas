# 运行、安全与观测

## 配置

所有配置通过环境变量读取，源码不自动读取 `.env`。
`Settings` 中密码字段使用 SecretStr，异常和事件不记录提示词、输入文档或凭据。
配置示例只列变量名。切换真实 DeepSeek provider 需配置 key 和账户实际可用 model，
本次开发不进行真实模型请求。

服务部署使用 PostgreSQL，先执行 Alembic 迁移再启动 worker。
多个 worker 使用同一数据库和相同的 handler 注册表，concurrency 控制每个进程槽位数。
当前版本可与 Python 3.12/3.13 环境配合，容器选择 3.13。

## 权限与输入

API 使用单租户 operator Bearer token。持有 token 的操作者可以创建工作流和审批，
本版没有每用户角色、租户隔离或细分 RBAC。不要将此 API 无保护地暴露到公网。
反向代理应提供 HTTPS 和请求频率限制，数据库不公开端口。

用户证据只能作为 JSON 文本上传，API 不接受服务器任意文件路径。
文件工具 root 必须是专用目录，且不能由不可信进程并发替换符号链接。
MCP 可运行外部代码，应只配置受信任的 server；默认不继承父进程凭据环境。
Docker sandbox 不挂载宿主文件、不联网、不继承密钥，镜像必须提前构建。
Docker daemon 权限仍属于高权限边界，应使用专用受控 worker 主机。

## 观测

`AGENTFLOW_TELEMETRY=false` 为默认值，无外部遥测上报。
开启后 API 在认证 `/metrics` 提供请求计数和延迟；worker 输出本地 OpenTelemetry
span。span 只包含 run/step 标识、尝试次数和异常类型，不记录异常正文或输入。
业务事件始终在数据库中，供恢复审计及 UI 时间线读取。

SSE：`GET /api/runs/{id}/events`，使用 Authorization header，并通过
`Last-Event-ID` 或 `after` 游标重放。可选 WebSocket 位于 API 服务的
`/api/runs/{id}/ws`，建立连接后 5 秒内发送首帧 `token` 和可选 `after` 字段。
不要将 token 放在 URL。Next.js 工作台使用 SSE；WebSocket 客户端直接连接 API。

可配置 `AGENTFLOW_TELEMETRY=true`、`AGENTFLOW_GRAFANA_PASSWORD` 后启用
`docker compose --profile observability up -d`。Prometheus 从 API 拉取指标，token
仅在容器启动时由环境写入权限受限的临时文件；Grafana 密码为空时拒绝启动。
Grafana 数据源已预配置，示例查询：`rate(agentflow_http_requests_total[5m])`。
不提供默认密码，也不把真实 token 写到 YAML。

## 备份与升级

停止接收新提交后允许在途步骤结束，停止 worker，再备份 PostgreSQL。
备份必须包含 workflows、runs、steps、approvals、events、artifacts 和迁移版本。
数据库保存输入原文和输出，备份访问控制、加密、保留与清理策略由部署者配置。
恢复后启动 worker，过期租约会自动重试；务必核对外部工具的幂等策略。

升级工作流时使用新的 version，升级 handler 行为时使用新注册名或保持旧实现。
迁移前先备份，在独立数据库验证升级及回退。回退初始迁移会删除数据，只能用于
专用测试数据库，不应作为生产数据恢复手段。

## 环境验收边界

GitHub Actions 已在 Linux/Python 3.12/3.13 上执行真实 PostgreSQL、Docker sandbox
和完整 Compose 业务冒烟，因此这些项目不再只是配置或 mock 验证。真实 DeepSeek
联调仍需在目标账户、网络与配额环境中单独进行。

正式上线还必须针对目标基础设施演练数据库备份恢复，并由外层网关提供 TLS、访问控制
与限流；这些环境责任不能由仓库 CI 代替。
