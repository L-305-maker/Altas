"""显式开启的本地 Prometheus 指标；没有云上报、输入内容或高基数运行 ID。"""

import math
import os
import time

from fastapi import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

from agentflow.domain.agent.status import AgentStatus


class RuntimeMetrics:
    """有限维度的运行信号，不作为持久事实或计费记录。"""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.workflow_rejections = Counter(
            "agentflow_workflow_admission_rejections_total",
            "Observed capacity rejections; process telemetry, not durable quota facts",
            registry=self.registry,
        )
        self.transitions = Counter(
            "agentflow_agent_transitions_total",
            "Committed agent transitions",
            ["state"],
            registry=self.registry,
        )
        self.models = Counter(
            "agentflow_agent_model_requests_total",
            "Committed model reservations and outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.retries = Counter(
            "agentflow_agent_model_retries_total",
            "Committed deferred model retries",
            registry=self.registry,
        )
        self.actions = Counter(
            "agentflow_agent_action_reservations_total",
            "Committed action reservations",
            registry=self.registry,
        )
        self.reviews = Counter(
            "agentflow_agent_reviews_total",
            "Committed action reviews",
            ["kind", "outcome"],
            registry=self.registry,
        )
        self.failures = Counter(
            "agentflow_runtime_failures_total",
            "Runtime failures by fixed category",
            ["kind"],
            registry=self.registry,
        )
        buckets = (0.005, 0.025, 0.1, 0.5, 2, 5, 30, 60)
        self.db_time = Histogram(
            "agentflow_store_transaction_seconds",
            "Transaction duration clipped to 60 seconds",
            ["outcome"],
            buckets=buckets,
            registry=self.registry,
        )
        self.execution_time = Histogram(
            "agentflow_worker_execution_seconds",
            "Execution duration clipped to 60 seconds",
            ["outcome"],
            buckets=buckets,
            registry=self.registry,
        )

    def record(self, kind, data):
        if kind == "workflow.admission_rejected":
            self.workflow_rejections.inc()
        elif kind == "agent.transition" and data.get("state") in {
            s.value for s in AgentStatus
        }:
            self.transitions.labels(data["state"]).inc()
            if data.get("error_code") == "AgentContextLimit":
                self.failures.labels("context_limit").inc()
        elif kind == "agent.model" and data.get("outcome") in {
            "reserved",
            "succeeded",
            "failed",
            "unknown",
        }:
            self.models.labels(data["outcome"]).inc()
        elif kind == "agent.retry_scheduled":
            self.retries.inc()
        elif (
            kind == "agent.action_reserved"
            and type(data.get("count")) is int
            and data["count"] > 0
        ):
            self.actions.inc(data["count"])
        elif kind == "agent.approval_requested":
            self.reviews.labels("approval", "requested").inc()
        elif kind == "agent.approval_decided" and data.get("decision") in {
            "approved",
            "rejected",
        }:
            self.reviews.labels("approval", data["decision"]).inc()
        elif kind == "agent.reconciliation_outcome" and data.get("status") in {
            "succeeded",
            "failed",
        }:
            outcome = (
                data["status"]
                if data.get("applied") is True
                else "audit_" + data["status"]
            )
            self.reviews.labels("reconciliation", outcome).inc()
        elif kind == "runtime.failure" and data.get("kind") in {
            "lost_lease",
            "state_conflict",
            "database",
            "lease_expired",
        }:
            self.failures.labels(data["kind"]).inc()

    def transaction(self, outcome, seconds):
        if outcome in {"committed", "error"} and math.isfinite(seconds):
            self.db_time.labels(outcome).observe(min(60, max(0, seconds)))

    def execution(self, outcome, seconds):
        if outcome in {"returned", "cancelled", "stopped", "error"} and math.isfinite(
            seconds
        ):
            self.execution_time.labels(outcome).observe(min(60, max(0, seconds)))


def install_metrics(app, dependencies):
    scrape_registry = count = latency = None
    try:
        runtime = app.state.store.metrics or RuntimeMetrics()
        app.state.store.metrics = runtime
        registry = CollectorRegistry()
        registry.register(runtime.registry)
        count = Counter(
            "agentflow_http_requests_total",
            "HTTP responses",
            ["method", "status"],
            registry=registry,
        )
        latency = Histogram(
            "agentflow_http_duration_seconds", "HTTP duration", registry=registry
        )
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
        scrape_registry = registry
    except Exception:
        pass  # Startup remains available even if an optional metrics directory fails.

    @app.middleware("http")
    async def measure(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        try:
            method = (
                request.method
                if request.method
                in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            status = (
                str(response.status_code)
                if 100 <= response.status_code <= 599
                else "OTHER"
            )
            count.labels(method, status).inc()
            latency.observe(min(60, time.monotonic() - start))
        except Exception:
            pass  # Optional metrics must not change the HTTP result.
        return response

    @app.get("/metrics", dependencies=dependencies, include_in_schema=False)
    def metrics():
        try:
            if scrape_registry is None:
                return Response("metrics unavailable", status_code=503)
            return Response(
                generate_latest(scrape_registry), media_type=CONTENT_TYPE_LATEST
            )
        except Exception:
            return Response("metrics unavailable", status_code=503)
