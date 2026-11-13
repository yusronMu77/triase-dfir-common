"""Trace id terpadu: log, X-Trace-Id, header keluar, NATS, dan span OpenTelemetry memakai trace id yang sama."""

from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from astria_common import tracing  # noqa: E402
from astria_common.app import create_app  # noqa: E402
from astria_common.http import TracingTransport, is_internal_host, make_client  # noqa: E402
from astria_common.settings import AstriaSettings  # noqa: E402

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN = "00f067aa0ba902b7"


def _app(*, otlp: bool = True):
    """otlp=True hanya di dalam fixture `otel` (provider sudah terpasang, jadi tidak ada exporter OTLP sungguhan)."""
    router = APIRouter()

    @router.get("/x")
    async def x() -> dict[str, str | None]:
        return {"log_trace_id": tracing.current_trace_id()}

    endpoint = "http://jaeger.invalid:4318" if otlp else None
    settings = AstriaSettings(service_name="uji", env="test", otlp_endpoint=endpoint)
    return create_app(settings, routers=[router])


def _server_span(exporter: InMemorySpanExporter):
    spans = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def test_trace_id_tanpa_header_sama_dengan_span_otel(otel) -> None:
    with TestClient(_app()) as client:
        resp = client.get("/x")
    span = _server_span(otel)
    tid = format(span.context.trace_id, "032x")
    assert resp.headers["X-Trace-Id"] == tid
    assert resp.json()["log_trace_id"] == tid


def test_traceparent_masuk_menjadi_induk(otel) -> None:
    with TestClient(_app()) as client:
        resp = client.get("/x", headers={"traceparent": f"00-{TRACE}-{PARENT_SPAN}-01"})
    span = _server_span(otel)
    assert resp.headers["X-Trace-Id"] == TRACE
    assert format(span.context.trace_id, "032x") == TRACE
    assert span.parent is not None and format(span.parent.span_id, "016x") == PARENT_SPAN


def test_x_trace_id_saja_tetap_dipakai(otel) -> None:
    with TestClient(_app()) as client:
        resp = client.get("/x", headers={"X-Trace-Id": TRACE})
    assert resp.headers["X-Trace-Id"] == TRACE
    assert format(_server_span(otel).context.trace_id, "032x") == TRACE


def test_x_trace_id_tidak_valid_diabaikan(otel) -> None:
    with TestClient(_app()) as client:
        resp = client.get("/x", headers={"X-Trace-Id": "0" * 32})
    assert resp.headers["X-Trace-Id"] == format(_server_span(otel).context.trace_id, "032x") != "0" * 32


def test_trace_span_akar_memakai_trace_id_amplop(otel) -> None:
    with tracing.trace_span("process contoh", kind="consumer", trace_id=TRACE) as span:
        assert tracing.active_span_trace_id() == TRACE
    assert span is not None and span.parent is None
    assert format(span.context.trace_id, "032x") == TRACE


def test_trace_span_induk_dari_header_nats(otel) -> None:
    carrier = {"traceparent": f"00-{TRACE}-{PARENT_SPAN}-01", "Nats-Msg-Id": "e1"}
    with tracing.trace_span("process contoh", kind="consumer", trace_id=TRACE, carrier=carrier) as span:
        pass
    assert format(span.parent.span_id, "016x") == PARENT_SPAN
    assert format(span.context.trace_id, "032x") == TRACE


def test_trace_span_abaikan_header_dengan_trace_lain(otel) -> None:
    carrier = {"traceparent": f"00-{'a' * 32}-{PARENT_SPAN}-01"}
    with tracing.trace_span("process contoh", trace_id=TRACE, carrier=carrier) as span:
        pass
    assert span.parent is None and format(span.context.trace_id, "032x") == TRACE


def test_trace_span_mencatat_galat(otel) -> None:
    with pytest.raises(RuntimeError), tracing.trace_span("gagal", trace_id=TRACE):
        raise RuntimeError("x")
    (span,) = otel.get_finished_spans()
    assert span.status.status_code.name == "ERROR"


@pytest.mark.anyio
async def test_klien_internal_meneruskan_span_klien(otel) -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="http://audit:8000", transport=TracingTransport(httpx.MockTransport(handler))
    ) as c:
        with tracing.trace_span("server", trace_id=TRACE):
            await c.get("/internal/v1/records")
    client_span = next(s for s in otel.get_finished_spans() if s.kind.name == "CLIENT")
    assert client_span.name == "GET audit"
    assert client_span.attributes["http.response.status_code"] == 204
    assert seen[0]["traceparent"] == f"00-{TRACE}-{format(client_span.context.span_id, '016x')}-01"
    assert seen[0]["x-trace-id"] == TRACE


@pytest.mark.anyio
async def test_klien_tanpa_otel_tetap_meneruskan_trace_id_log() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(204)

    token = tracing.bind_trace(TRACE)
    try:
        async with httpx.AsyncClient(
            base_url="http://audit:8000", transport=TracingTransport(httpx.MockTransport(handler))
        ) as c:
            await c.get("/x")
    finally:
        tracing.reset_trace(token)
    assert seen[0]["x-trace-id"] == TRACE
    assert seen[0]["traceparent"].startswith(f"00-{TRACE}-")


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("audit", True),
        ("localhost", True),
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("[::1]", True),
        ("svc.astria.internal", True),
        ("api.telegram.org", False),
        ("8.8.8.8", False),
        ("", False),
    ],
)
def test_host_internal(host: str, internal: bool) -> None:
    assert is_internal_host(host) is internal


def test_make_client_hanya_menelusur_host_internal() -> None:
    assert isinstance(make_client("http://audit:8000")._transport, TracingTransport)
    assert not isinstance(make_client("https://api.telegram.org")._transport, TracingTransport)
    assert isinstance(make_client("https://api.telegram.org", propagate_trace=True)._transport, TracingTransport)
    assert not isinstance(make_client("http://audit:8000", propagate_trace=False)._transport, TracingTransport)


def test_histogram_latensi_http_di_metrics() -> None:
    with TestClient(_app(otlp=False)) as client:
        client.get("/x")
        client.get("/metrics")
        body = client.get("/metrics").text
    assert 'astria_http_request_duration_seconds_count{method="GET",route="/x"}' in body
    assert 'astria_http_request_duration_seconds_count{method="GET",route="/metrics"}' not in body
    assert not tracing.otel_enabled()
