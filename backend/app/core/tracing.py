"""
OpenTelemetry tracing setup for Phoenix standalone
"""
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_tracing_initialized = False
_provider: TracerProvider | None = None


def setup_tracing() -> None:
    """Initialize Phoenix tracing via OTLP exporter"""
    global _tracing_initialized, _provider

    if _tracing_initialized or not settings.PHOENIX_ENABLE_TRACING:
        return

    try:
        resource = Resource.create({
            "service.name": "fibre-forecast-backend",
            "service.version": "1.0.0",
            "deployment.environment": "development",
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.PHOENIX_COLLECTOR_ENDPOINT}/v1/traces",
            timeout=10,
        )
        # BatchSpanProcessor funnels every span through one thread-safe queue and a
        # single background export thread. This is required because parts of the
        # pipeline (e.g. the RAG enrichment ThreadPoolExecutor) end spans concurrently;
        # SimpleSpanProcessor's synchronous per-span HTTP export races under that
        # concurrency and silently drops spans (orphaned parents, missing LLM spans).
        # A short schedule delay keeps short-lived spans (forecast, recommendations)
        # visible quickly; call flush_tracing() on shutdown / after async jobs to
        # force-drain the queue.
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                schedule_delay_millis=500,
                max_queue_size=2048,
                max_export_batch_size=512,
            )
        )
        trace.set_tracer_provider(provider)
        _provider = provider

        # Instrument libraries
        SQLAlchemyInstrumentor().instrument()

        _tracing_initialized = True
        logger.info(f"✓ Tracing enabled to {settings.PHOENIX_COLLECTOR_ENDPOINT} (service=fibre-forecast-backend)")
    except Exception as e:
        logger.warning(f"⚠️ Tracing setup failed: {e}")
        _tracing_initialized = False  # Fail gracefully


def get_tracer(name: str = "forecast-backend"):
    """Get a tracer instance"""
    setup_tracing()
    return trace.get_tracer(name)


def flush_tracing(timeout_millis: int = 5000) -> None:
    """Force-drain any queued spans to Phoenix.

    Call after a background job whose spans should appear immediately, and on
    application shutdown so nothing is lost when the export thread is stopped.
    """
    if _provider is None:
        return
    try:
        _provider.force_flush(timeout_millis)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Trace flush failed: %s", exc)
