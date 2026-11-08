"""Trace id per permintaan dan per event, plus OpenTelemetry opsional (extra `otel`).

Trace id diambil dari header `traceparent` (W3C) atau `X-Trace-Id`, atau dibuat baru. Nilainya ikut di
setiap baris log (structlog contextvars), di header jawaban `X-Trace-Id`, dan di `trace_id` amplop event.
"""

from __future__ import annotations

import contextvars
import re
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("astria_trace_id", default=None)
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def bind_trace(trace_id: str, **correlation: object) -> contextvars.Token[str | None]:
    """Pasang trace id (dan korelasi) ke konteks saat ini, mis. saat memproses event."""
    structlog.contextvars.bind_contextvars(trace_id=trace_id, **({"correlation": correlation} if correlation else {}))
    return _TRACE_ID.set(trace_id)


def reset_trace(token: contextvars.Token[str | None]) -> None:
    _TRACE_ID.reset(token)
    structlog.contextvars.unbind_contextvars("trace_id", "correlation")


def trace_id_from_headers(traceparent: str | None, x_trace_id: str | None) -> str | None:
    if traceparent and (m := _TRACEPARENT.match(traceparent.strip().lower())):
        return m.group(1)
    if x_trace_id and _HEX32.match(x_trace_id.strip().lower()):
        return x_trace_id.strip().lower()
    return None


def install_trace_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _trace(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        trace_id = (
            trace_id_from_headers(request.headers.get("traceparent"), request.headers.get("x-trace-id"))
            or new_trace_id()
        )
        token = bind_trace(trace_id)
        try:
            response = await call_next(request)
        finally:
            reset_trace(token)
        response.headers["X-Trace-Id"] = trace_id
        return response


def setup_otel(app: FastAPI, service: str, endpoint: str | None) -> bool:
    """Aktifkan ekspor OTLP HTTP bila endpoint diisi dan extra `otel` terpasang. Mengembalikan True bila aktif."""
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        structlog.get_logger(__name__).warning("otel_tidak_terpasang", endpoint=endpoint)
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": f"astria-{service}"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,metrics")
    return True
