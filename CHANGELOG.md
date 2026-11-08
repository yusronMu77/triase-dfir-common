# Changelog

## [Unreleased]

## [0.1.0] - 2026-11-08

### Added

- Paket `astria_common`: pengaturan dan pagar `ASTRIA_ENV`, `create_app()` dengan `/health`, `/ready`, `/metrics`, log JSON, propagasi trace, galat seragam.
- Database async per skema, `EventBus` JetStream, outbox transaksional dengan relay, konsumen durable idempoten dengan backoff dan DLQ.
- Autentikasi layanan: HMAC-SHA256 per permintaan, token JWT EdDSA dengan JWKS, `require_service()`, klien token; klien audit sinkron fail-closed dan event audit asinkron.
- Templat layanan (`template/service`, `template/render.py`): kode awal, Dockerfile multi-tahap non-root, CI dengan smoke `/ready`; `template/localtest.sh` untuk uji terhadap salinan kerja.
- Uji unit dan integrasi (PostgreSQL dan NATS sungguhan di CI).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
