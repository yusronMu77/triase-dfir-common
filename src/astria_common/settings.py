"""Konfigurasi dasar layanan dari variabel lingkungan `ASTRIA_*`.

Layanan menurunkan kelas ini untuk field miliknya sendiri. Variabel tak dikenal dengan prefiks `ASTRIA_`
di berkas env ditolak (`extra="forbid"`), dan `ASTRIA_ENV` wajib diisi: tidak ada nilai bawaan diam-diam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "demo", "prod", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class AstriaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASTRIA_", extra="forbid", frozen=True)

    service_name: str
    env: Env
    version: str = "0.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: LogLevel = "INFO"

    database_url: SecretStr | None = None
    """URL SQLAlchemy, mis. postgresql+psycopg://user:pw@postgres:5432/ingest_dev. Hanya akun layanan ini."""
    db_schema: str | None = None
    """Skema PostgreSQL milik layanan (bila satu basis data dipakai bersama). Kosong = skema bawaan."""

    nats_url: str | None = None
    otlp_endpoint: str | None = None
    """Kosong = trace OpenTelemetry dimatikan."""

    identity_url: str = "http://identity:8000"
    audit_url: str = "http://audit:8000"
    service_key: SecretStr | None = None
    service_key_file: Path | None = None
    """Kunci HMAC awal layanan ini (hex). Dipakai untuk meminta token layanan ke identity."""

    def service_key_bytes(self) -> bytes | None:
        """Kunci layanan dari berkas secret (diutamakan) atau dari env."""
        if self.service_key_file is not None:
            return bytes.fromhex(self.service_key_file.read_text(encoding="utf-8").strip())
        if self.service_key is not None:
            return bytes.fromhex(self.service_key.get_secret_value().strip())
        return None

    @property
    def principal(self) -> str:
        """Nama layanan sebagai subjek token, mis. `svc:ingest`."""
        return f"svc:{self.service_name}"
