"""Klien HTTP keluar: batas waktu pasti dan tanpa redirect (pola anti-SSRF).

Klien ke host internal (nama layanan compose seperti `audit`, `localhost`, IP privat, `*.internal`) meneruskan
konteks trace (`traceparent`, `X-Trace-Id`) dan, bila OpenTelemetry aktif, membuka span klien per permintaan.
Host lain (mis. `api.telegram.org`) tidak menerima header trace kecuali `propagate_trace=True`.
"""

from __future__ import annotations

import ipaddress

import httpx

from astria_common.tracing import outbound_trace_headers, trace_span

_INTERNAL_SUFFIXES = (".internal", ".local", ".localhost", ".svc", ".cluster.local")


def is_internal_host(host: str) -> bool:
    host = host.strip("[]").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES)
    return ip.is_loopback or ip.is_private


class TracingTransport(httpx.AsyncBaseTransport):
    """Membungkus transport: span klien dan header trace di setiap permintaan."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attributes = {
            "http.request.method": request.method,
            "server.address": request.url.host,
            "url.path": request.url.path,
        }
        with trace_span(f"{request.method} {request.url.host}", kind="client", attributes=attributes) as span:
            for name, value in outbound_trace_headers().items():
                request.headers[name] = value
            response = await self._inner.handle_async_request(request)
            if span is not None:
                span.set_attribute("http.response.status_code", response.status_code)
                if response.status_code >= 500:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(Status(StatusCode.ERROR, f"HTTP {response.status_code}"))
            return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_client(
    base_url: str = "",
    *,
    timeout: float = 5.0,
    propagate_trace: bool | None = None,
) -> httpx.AsyncClient:
    """`propagate_trace=None`: otomatis, hanya untuk host internal (lihat `is_internal_host`)."""
    if propagate_trace is None:
        propagate_trace = bool(base_url) and is_internal_host(httpx.URL(base_url).host)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout, connect=3.0),
        follow_redirects=False,
        headers={"User-Agent": "astria-service"},
        transport=TracingTransport(httpx.AsyncHTTPTransport()) if propagate_trace else None,
    )
