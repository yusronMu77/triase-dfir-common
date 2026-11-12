# Changelog

## [Unreleased]

## [0.2.3] - 2026-11-12

### Changed

- Dependensi `astria-contracts` naik dari `v0.3.0` ke `v0.4.0` (OpenAPI identity, audit, config; event `identity.revoked` dan `config.changed`). Tidak ada perubahan kode. Layanan yang memakai `override-dependencies` untuk contracts v0.4.0 bisa menghapusnya.

## [0.2.2] - 2026-11-10

### Changed

- Dependensi `astria-contracts` naik dari `v0.2.0` ke `v0.3.0` (event `digest.published`, kunci correlation `digest_id` dan `case_id`). Tidak ada perubahan kode.

## [0.2.1] - 2026-11-10

### Changed

- Dependensi `astria-contracts` naik dari `v0.1.0` ke `v0.2.0` (event `narration.retry_requested`, field opsional `kode.teknik_attack`/`kode.temuan_korelasi` dan `evidence.ingested.file_sha256`/`batch_sha256`). Tanpa ini layanan yang memakai contracts v0.2.0 tidak bisa di-lock karena URL git contracts bentrok. Tidak ada perubahan kode.

## [0.2.0] - 2026-11-08

### Added

- Basis data dua dialek (keputusan pemilik 2026-11-08): PostgreSQL di server (psycopg 3) dan SQLite untuk proses lokal (aiosqlite, WAL, `foreign_keys=ON`, `busy_timeout`). Dialek dipilih dari skema `ASTRIA_DATABASE_URL`; `Database.dialect` dan `Database.name` (`postgres` atau `sqlite`) tampil di `/ready`.
- `dialect_of()` dan `normalize_url()` di `astria_common.db`; `ASTRIA_DATABASE_URL` dengan dialek selain PostgreSQL atau SQLite ditolak saat konfigurasi dibaca.
- Pagar `ASTRIA_ENV=prod` menolak SQLite; nama basis data SQLite diambil dari nama berkas (akhiran `_dev` atau `_demo` tetap diperiksa).
- Dependensi `aiosqlite>=0.22.0,<1`.

### Changed

- `Database.name` kini instance, bukan konstanta kelas. `schema` diabaikan di SQLite.

## [0.1.0] - 2026-11-08

### Added

- Paket `astria_common`: pengaturan dan pagar `ASTRIA_ENV`, `create_app()` dengan `/health`, `/ready`, `/metrics`, log JSON, propagasi trace, galat seragam.
- Database async per skema, `EventBus` JetStream, outbox transaksional dengan relay, konsumen durable idempoten dengan backoff dan DLQ.
- Autentikasi layanan: HMAC-SHA256 per permintaan, token JWT EdDSA dengan JWKS, `require_service()`, klien token; klien audit sinkron fail-closed dan event audit asinkron.
- Templat layanan (`template/service`, `template/render.py`): kode awal, Dockerfile multi-tahap non-root, CI dengan smoke `/ready`; `template/localtest.sh` untuk uji terhadap salinan kerja.
- Uji unit dan integrasi (PostgreSQL dan NATS sungguhan di CI).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
