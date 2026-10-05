FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.13 && useradd --uid 10001 --create-home agentflow
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev && chown -R agentflow:agentflow /app
USER agentflow
EXPOSE 8000
CMD ["python", "-m", "agentflow.interfaces.cli.main", "api", "--host", "0.0.0.0"]
