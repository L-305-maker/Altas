# V1.0 验证记录

日期：2026-10-05。环境：Windows、Python 3.12.13、Node.js 24.18.0。
所有模型相关检查均离线执行，没有使用真实 DeepSeek 或其他模型 API。

| 检查 | 实测结果 |
|---|---|
| 完整 pytest | 81 passed，5 skipped；跳过项为 3 个 PostgreSQL 与 2 个 Docker 专项 |
| 临时真实 PostgreSQL | 3 passed；并发领取、旧 token 拒收、迁移升级/回退/模型一致性 |
| 官方 MCP SDK stdio | 真子进程启动、工具发现与 fingerprint 调用通过 |
| 离线业务评测 | 3/3，引文来源追溯率 1.0；不代表模型推理质量 |
| SQLite 100 步链基准 | 约 1.016 秒，98.5 步/秒；仅为本机微基准 |
| Next.js 生产构建 | 通过，含 TypeScript 校验 |
| 前端生产依赖 npm audit | 0 vulnerabilities |
| Python 发布包 | wheel 与源码 tar.gz 构建成功 |
| Docker Compose | 配置校验通过；未运行容器服务 |
| Ruff | 代码检查与格式检查通过 |
| Playwright 浏览器 | 创建草稿、等待审批、审批通过、保存版本；桌面与 390px 移动布局检查 |
| 最终界面浏览器控制台 | 0 errors，0 warnings |

常规 pytest 的 PostgreSQL skip 不代表未验证，已通过独立临时实例脚本执行。
测试数据库只绑定 127.0.0.1，使用随机端口，结束后关闭并清理。

尚未执行：本机 Docker daemon 未运行，因此真实容器沙箱和完整 Compose 服务启动
未完成验证；已提供专用 Docker CI job 和显式 opt-in 测试。真实 DeepSeek 联调
按用户要求延后。CI 中 Linux/Python 3.13 的结果需由实际 CI 运行确认。

FastAPI/Starlette 测试客户端当前有一条上游 HTTPX 弃用警告，不影响断言；
未为了隐藏警告而屏蔽测试输出。

测试不能证明系统在所有生产负载或故障下正确。上线前仍需依据目标环境验证
容器运行、数据库备份恢复、TLS、权限和真实模型行为。
