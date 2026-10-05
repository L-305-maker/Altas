"""AgentFlow：从本地 DAG 到持久执行、Agent 工具和人工审批的学习型引擎。

阅读顺序见 docs/learning-guide.md。V0.1 的 LocalExecutor/Scheduler 保留用于
理解调度原理；服务部署使用 Worker + Store，二者没有隐式切换或状态混用。
"""

__version__ = "1.0.0"
