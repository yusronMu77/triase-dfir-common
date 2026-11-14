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
| `consumer` | Konsumen pull durable yang idempoten (`processed_events`), coba ulang dengan backoff, DLQ `astria.dlq.<konsumen>`; `PermanentError` untuk galat yang tidak perlu diulang |
| `hmac_auth` | Tanda tangan HMAC-SHA256 per permintaan (jalur Gateway, Collector, penerbitan token) |
| `auth` | Token layanan JWT EdDSA 300 detik, JWKS, `require_service(audience, allowed_callers)`, `ServiceTokenClient` |
| `audit_client` | `AuditClient.record()` sinkron fail-closed untuk aksi kritis; `audit_event()` asinkron lewat outbox |
| `service` | `standard_runtime()`: DB, bus, dan relay dalam satu `Runtime` |
| `tracing` | Satu trace id untuk log, `X-Trace-Id`, galat, amplop event, dan Jaeger; `setup_otel` (OTLP HTTP ke `ASTRIA_OTLP_ENDPOINT`), `trace_span()`, `outbound_trace_headers()` |
| `http` | `make_client()`: batas waktu, tanpa redirect; host internal mendapat `traceparent`/`X-Trace-Id` dan span klien, host luar tidak (`propagate_trace`) |
| `metrics` | Metrik Prometheus bersama, termasuk histogram `astria_http_request_duration_seconds` dan `astria_event_handle_duration_seconds` |

### Galat permanen di konsumen

Semua galat handler dianggap sementara: pesan di-nak dengan backoff (1, 5, 30, 120 detik) dan baru masuk DLQ setelah
`max_deliver` kali gagal. Untuk galat yang pasti sama di setiap percobaan (mis. data event tidak valid), handler
melempar `PermanentError` supaya event langsung diterbitkan ke DLQ:

```python
from astria_common.consumer import PermanentError


class KartuTidakValid(PermanentError, ValueError):  # tetap ValueError untuk kode lain yang sudah menangkapnya
    ...


async def handle(conn, envelope, data) -> None:
    if data.kode.band not in BANDS:
        raise KartuTidakValid(f"kode.band tidak dikenal: {data.kode.band!r}")
    ...  # galat lain (database, jaringan) tetap sementara dan dicoba ulang
```

- Klasifikasi permanen harus eksplisit; jangan menangkap `Exception` lalu membungkusnya. Galat database dan jaringan tetap sementara.
- Transaksi handler dibatalkan, jadi `processed_events` tidak terisi. Record DLQ memuat `reason` (`permanen:<NamaKelas>`), `detail` (pesan galat, maks 500 karakter), `event_id`, `trace_id`, dan `payload` (amplop utuh). Pesan galat tidak boleh memuat rahasia atau data mentah.
- Urutannya: terbit ke DLQ, lalu ack pesan asli. Bila penerbitan DLQ gagal, pesan asli tidak di-ack: di-nak berjeda dan dicoba lagi, sampai `max_deliver` habis. `Nats-Msg-Id` DLQ diturunkan dari stream_seq, jadi penerbitan ulang disaring JetStream. Bila pengiriman terakhir pun gagal, pesan tetap di stream tanpa ack, dan `stream_seq` ada di log `event_surat_mati_gagal` untuk diputar ulang manual.
- Metrik `astria_events_consumed_total` mendapat hasil `permanent` (selain `ok`, `duplicate`, `retry`, `dead_letter`).

### Trace

Dengan extra `otel` dan `ASTRIA_OTLP_ENDPOINT` terisi, trace id permintaan sama dengan trace id span OpenTelemetry,
jadi `trace_id` dari log atau galat bisa langsung dibuka di Jaeger. `traceparent` masuk dipakai sebagai induk;
`X-Trace-Id` saja juga dihormati. `make_client` ke host internal membuka span klien dan meneruskan konteks. Pesan NATS
membawa `traceparent`; relay outbox membuka span `publish <jenis>` dengan trace id amplop (span akar karena outbox
memisahkan waktu terbit dari permintaan asal) dan konsumen membuka span `process <jenis>` sebagai anaknya. Tanpa OTel,
trace id tetap diteruskan lewat header dan amplop untuk korelasi log.

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
