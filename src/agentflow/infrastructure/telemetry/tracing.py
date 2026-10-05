"""显式开启时输出本地 OpenTelemetry span，默认不配置任何网络 exporter。"""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


def local_tracing() -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "agentflow-worker"})
    )
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider
