"""Pustaka bersama layanan Astria.

Modul utama:
- settings, envguard: konfigurasi `ASTRIA_*` dan pagar `ASTRIA_ENV` (layanan menolak mulai bila bertentangan).
- app: `create_app` memasang /health, /ready, /metrics, galat seragam, trace id, dan siklus komponen.
- db, events, outbox, consumer: PostgreSQL, NATS JetStream, outbox transaksional, konsumen idempoten.
- auth, hmac_auth: token layanan JWT EdDSA (Core) dan HMAC per permintaan (Gateway, Collector, penerbitan token).
- audit_client: jalur audit sinkron (fail-closed) dan asinkron (event `audit.record` lewat outbox).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("astria-common")
except PackageNotFoundError:  # pragma: no cover - dijalankan dari sumber tanpa instalasi
    __version__ = "0.0.0+src"
