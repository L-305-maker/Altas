"""显式开启的本地 Prometheus 指标；没有云上报、输入内容或高基数运行 ID。"""

import time

from fastapi import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


def install_metrics(app, dependencies):
    registry = CollectorRegistry()
    count = Counter(
        "agentflow_http_requests_total",
        "HTTP responses",
        ["method", "status"],
        registry=registry,
    )
    latency = Histogram(
        "agentflow_http_duration_seconds", "HTTP duration", registry=registry
    )

    @app.middleware("http")
    async def measure(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        count.labels(request.method, str(response.status_code)).inc()
        latency.observe(time.monotonic() - start)
        return response

    @app.get("/metrics", dependencies=dependencies, include_in_schema=False)
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
