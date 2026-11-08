<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria common

Pustaka bersama layanan Astria Core (`astria_common`) dan templat layanan. Setiap layanan Python memakai pustaka ini lewat tag git.

> Status: Tahap 0, v0.1.0. Repo ini adalah submodule dari workspace `triase-dfir` (folder `common/`). Rancangan: `notes/2026-11-06-triase-dfir-app-common-dan-infra.md` di repo root. Lajur developer: 3.

## Tech stack

Python 3.14, uv 0.13.7, FastAPI, pydantic-settings, structlog, prometheus-client, PyJWT (EdDSA), nats-py (JetStream), SQLAlchemy 2.1 async + psycopg 3. OpenTelemetry opsional (extra `otel`).

## Modul

| Modul | Isi |
|---|---|
| `settings` | `AstriaSettings` (awalan env `ASTRIA_`); `ASTRIA_ENV` wajib: `dev`, `demo`, `prod`, `test` |
| `envguard` | Pagar lingkungan: layanan menolak start bila konfigurasi bertentangan dengan `ASTRIA_ENV` |
| `app` | `create_app()`: `/health`, `/ready` (503 bila komponen wajib turun), `/metrics`, log JSON, trace, galat seragam |
| `db` | `Database`: engine async, skema per layanan, siapkan tabel di latar dengan coba ulang |
| `events` | Amplop event, `EventBus` (JetStream, stream `ASTRIA`, `Nats-Msg-Id` = id event untuk dedupe) |
| `outbox` | Outbox transaksional (`enqueue` dalam transaksi bisnis) dan `OutboxRelay` |
| `consumer` | Konsumen pull durable yang idempoten (`processed_events`), coba ulang dengan backoff, DLQ `astria.dlq.<konsumen>` |
| `hmac_auth` | Tanda tangan HMAC-SHA256 per permintaan (jalur Gateway, Collector, penerbitan token) |
| `auth` | Token layanan JWT EdDSA 300 detik, JWKS, `require_service(audience, allowed_callers)`, `ServiceTokenClient` |
| `audit_client` | `AuditClient.record()` sinkron fail-closed untuk aksi kritis; `audit_event()` asinkron lewat outbox |
| `service` | `standard_runtime()`: DB, bus, dan relay dalam satu `Runtime` |

## Layanan baru dari templat

```text
python template/render.py ../<layanan> --service <layanan> --description "..."
python template/render.py ../<layanan> --service <layanan> --managed-only   # hanya Dockerfile, .dockerignore, CI
```

Berkas terkelola (`Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`) selalu ditulis ulang; ubah di templat, lalu render ulang ke semua layanan. Berkas kode hanya ditulis bila belum ada.

## Perintah

```text
uv sync --all-extras
uv run ruff format --check . && uv run ruff check .
uv run pytest                    # uji integrasi dilewati tanpa env di bawah
```

Uji integrasi butuh PostgreSQL dan NATS JetStream: `ASTRIA_TEST_DATABASE_URL` dan `ASTRIA_TEST_NATS_URL`. Di devenv keduanya tersedia di kontainer `py`. Untuk mengembangkan common dan satu layanan bersamaan tanpa tag baru: `sh ../common/template/localtest.sh` dari folder layanan di kontainer `py`.

Rilis = tag git `vX.Y.Z` di `main`; layanan menaikkan tag di `[tool.uv.sources]`, lalu `uv lock`.

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.
