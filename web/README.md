# Living Guideline application UI

This Next.js evidence workbench belongs to the Living Guideline reference application.
It is not a generic AgentFlow console. It uses the application host HTTP API, including
`/api/guidelines`, through the same-origin proxy. The Python core does not depend on it.

The `web/` path is retained for the existing Docker build context. This UI can be replaced
independently of AgentFlow. Start the backend with `uv run living-guideline api` and
`uv run living-guideline worker`; see the repository README for environment configuration.
