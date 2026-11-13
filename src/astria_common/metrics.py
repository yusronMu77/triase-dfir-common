"""Metrik Prometheus bersama. Endpoint /metrics dipasang oleh create_app."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

SERVICE_INFO = Info("astria_service", "Identitas layanan")
EVENTS_PUBLISHED = Counter("astria_events_published_total", "Event yang diterbitkan ke JetStream", ["type", "result"])
EVENTS_CONSUMED = Counter("astria_events_consumed_total", "Event yang diproses konsumen", ["consumer", "result"])
EVENT_HANDLE_SECONDS = Histogram(
    "astria_event_handle_duration_seconds",
    "Lama satu event diproses konsumen (transaksi handler sampai ack atau nak)",
    ["consumer"],
    buckets=_LATENCY_BUCKETS,
)
OUTBOX_BACKLOG = Gauge("astria_outbox_backlog", "Baris outbox yang belum terbit")
OUTBOX_OLDEST_SECONDS = Gauge("astria_outbox_oldest_seconds", "Umur baris outbox tertua yang belum terbit")
HTTP_REQUESTS = Counter("astria_http_requests_total", "Permintaan HTTP", ["method", "route", "status"])
HTTP_REQUEST_SECONDS = Histogram(
    "astria_http_request_duration_seconds",
    "Lama permintaan HTTP sampai header jawaban dikirim (SSE: sampai aliran dibuka)",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
