"""Metrik Prometheus bersama. Endpoint /metrics dipasang oleh create_app."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

SERVICE_INFO = Info("astria_service", "Identitas layanan")
EVENTS_PUBLISHED = Counter("astria_events_published_total", "Event yang diterbitkan ke JetStream", ["type", "result"])
EVENTS_CONSUMED = Counter("astria_events_consumed_total", "Event yang diproses konsumen", ["consumer", "result"])
OUTBOX_BACKLOG = Gauge("astria_outbox_backlog", "Baris outbox yang belum terbit")
OUTBOX_OLDEST_SECONDS = Gauge("astria_outbox_oldest_seconds", "Umur baris outbox tertua yang belum terbit")
HTTP_REQUESTS = Counter("astria_http_requests_total", "Permintaan HTTP", ["method", "route", "status"])
