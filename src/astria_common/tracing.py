"""Trace id per permintaan dan per event, plus OpenTelemetry opsional (extra `otel`).

Satu trace id dipakai di semua tempat: baris log (structlog contextvars), header jawaban `X-Trace-Id`,
`trace_id` di galat dan amplop event, dan trace di Jaeger. Urutan sumbernya:

1. span OpenTelemetry yang aktif (bila OTel aktif; span server mengambil `traceparent` atau `X-Trace-Id` masuk);
2. header `traceparent` (W3C) atau `X-Trace-Id`;
3. trace id baru.

Panggilan HTTP internal (`http.make_client`) meneruskan `traceparent` dan `X-Trace-Id`. Pesan NATS membawa
`traceparent` di header, dan relay outbox serta konsumen membuka span dengan trace id amplop, sehingga satu alur
event (ingest, triage, narrative, ...) tampil sebagai satu trace di Jaeger.
"""

from __future__ import annotations

import contextvars
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

import structlog
from fastapi import FastAPI, Request, Response

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("astria_trace_id", default=None)
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

SpanKindName = Literal["internal", "server", "client", "producer", "consumer"]

_otel_enabled = False


def otel_enabled() -> bool:
    """True bila TracerProvider Astria sudah dipasang di proses ini."""
    return _otel_enabled


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


def active_span_trace_id() -> str | None:
    """Trace id span OpenTelemetry yang aktif, atau None bila OTel mati atau tidak ada span."""
    if not _otel_enabled:
        return None
    from opentelemetry import trace

    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def resolve_trace_id(traceparent: str | None, x_trace_id: str | None) -> str:
    """Trace id untuk permintaan masuk: span aktif, lalu header, lalu baru (lihat docstring modul)."""
    return active_span_trace_id() or trace_id_from_headers(traceparent, x_trace_id) or new_trace_id()


def install_trace_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _trace(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        trace_id = resolve_trace_id(request.headers.get("traceparent"), request.headers.get("x-trace-id"))
        token = bind_trace(trace_id)
        try:
            response = await call_next(request)
        finally:
            reset_trace(token)
        response.headers["X-Trace-Id"] = trace_id
        return response


def outbound_trace_headers() -> dict[str, str]:
    """Header untuk panggilan keluar ke layanan internal.

    OTel aktif: `traceparent` dari span saat ini (span klien bila dipanggil di dalam `trace_span`) dan `X-Trace-Id`.
    OTel mati: `traceparent` sintetis dari trace id log saat ini, supaya penerima tetap mencatat trace id yang sama.
    """
    if _otel_enabled:
        from opentelemetry import propagate

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        if carrier:
            return carrier
    trace_id = current_trace_id()
    if trace_id is None or not _HEX32.match(trace_id):
        return {}
    return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01", "x-trace-id": trace_id}


@contextmanager
def trace_span(
    name: str,
    *,
    kind: SpanKindName = "internal",
    trace_id: str | None = None,
    carrier: Mapping[str, str] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """Buka span OpenTelemetry; no-op (menghasilkan None) bila OTel mati.

    Induk span: konteks dari `carrier` (mis. header NATS) bila trace id-nya cocok dengan `trace_id`, lalu span
    aktif bila trace id-nya cocok, selain itu span akar baru yang memakai `trace_id` (bila diberikan).
    """
    if not _otel_enabled:
        yield None
        return
    from opentelemetry import propagate, trace
    from opentelemetry.context import Context
    from opentelemetry.trace import SpanKind, Status, StatusCode

    from astria_common._otel import FORCED_TRACE_ID, parse_trace_id

    wanted = parse_trace_id(trace_id.lower()) if trace_id else None
    parent: Context | None = None  # None = konteks aktif
    if carrier:
        extracted = propagate.extract(dict(carrier))
        remote = trace.get_current_span(extracted).get_span_context()
        if remote.is_valid and (wanted is None or remote.trace_id == wanted):
            parent = extracted
    if parent is None and wanted is not None:
        active = trace.get_current_span().get_span_context()
        if not (active.is_valid and active.trace_id == wanted):
            parent = Context()  # span akar dengan trace id yang dipaksakan

    forced = FORCED_TRACE_ID.set(wanted)
    try:
        span = trace.get_tracer("astria_common").start_span(
            name, context=parent, kind=SpanKind[kind.upper()], attributes=dict(attributes or {})
        )
    finally:
        FORCED_TRACE_ID.reset(forced)
    with trace.use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def record_span_error(exc: BaseException) -> None:
    """Tandai span aktif gagal untuk galat yang ditangani (mis. event di-nak); no-op bila OTel mati."""
    if not _otel_enabled:
        return
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def setup_otel(
    app: FastAPI,
    service: str,
    endpoint: str | None,
    *,
    version: str | None = None,
    env: str | None = None,
) -> bool:
    """Aktifkan ekspor OTLP HTTP bila endpoint diisi dan extra `otel` terpasang. Mengembalikan True bila aktif.

    TracerProvider dipasang sekali per proses; aplikasi berikutnya di proses yang sama hanya diinstrumentasi.
    """
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        from astria_common import _otel
    except ImportError:
        structlog.get_logger(__name__).warning("otel_tidak_terpasang", endpoint=endpoint)
        return False
    if not _otel_enabled:
        attributes = {"service.version": version or "", "deployment.environment.name": env or ""}
        _otel.install_provider(
            service,
            exporter=OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"),
            attributes={k: v for k, v in attributes.items() if v},
        )
        enable_otel()
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,metrics")
    return True


def enable_otel(enabled: bool = True) -> None:
    """Tandai OTel aktif (dipanggil `setup_otel`; uji memakainya dengan provider dan exporter sendiri)."""
    global _otel_enabled
    _otel_enabled = enabled
