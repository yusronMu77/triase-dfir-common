"""Pagar `ASTRIA_ENV`: layanan menolak mulai bila konfigurasinya bertentangan dengan lingkungannya.

Aturan dari note arsitektur tiga lingkungan (bagian 2):

| ASTRIA_ENV | Wajib | Dilarang |
|---|---|---|
| dev  | nama basis data `*_dev` | basis data `*_demo` atau tanpa akhiran lingkungan |
| demo | nama basis data `*_demo` | basis data permanen (`*_dev` atau tanpa akhiran), Collector |
| prod | login aktif, PostgreSQL | backend replay, SQLite, kunci API model, basis data dev/demo, endpoint dev |
| test | - | - (hanya untuk uji otomatis) |

Field khusus layanan diperiksa bila ada di kelas settings turunan: `model_backend`, `model_api_key`,
`auth_disabled`, `dev_endpoints`, `collector_enabled`.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from astria_common.settings import AstriaSettings


class EnvironmentGuardError(RuntimeError):
    """Konfigurasi bertentangan dengan ASTRIA_ENV; proses harus berhenti sebelum membuka port."""

    def __init__(self, env: str, problems: list[str]) -> None:
        self.env = env
        self.problems = problems
        super().__init__(f"ASTRIA_ENV={env} menolak mulai: " + "; ".join(problems))


def is_sqlite(settings: AstriaSettings) -> bool:
    return settings.database_url is not None and settings.database_url.get_secret_value().startswith("sqlite")


def database_name(settings: AstriaSettings) -> str | None:
    """Nama basis data: segmen path PostgreSQL, atau nama berkas tanpa ekstensi untuk SQLite."""
    if settings.database_url is None:
        return None
    url = settings.database_url.get_secret_value()
    path = urlsplit(url).path
    if is_sqlite(settings):
        stem = PurePosixPath(path.replace("\\", "/")).stem
        return stem or None
    return path.lstrip("/") or None


def _opt(settings: AstriaSettings, name: str) -> Any:
    return getattr(settings, name, None)


def violations(settings: AstriaSettings) -> list[str]:
    env = settings.env
    db = database_name(settings)
    problems: list[str] = []

    if env == "dev":
        if db is not None and not db.endswith("_dev"):
            problems.append(f"basis data '{db}' harus berakhiran _dev")
    elif env == "demo":
        if db is not None and not db.endswith("_demo"):
            problems.append(
                f"basis data '{db}' harus berakhiran _demo (data demo dari samples, bukan basis data permanen)"
            )
        if _opt(settings, "collector_enabled"):
            problems.append("Collector tidak boleh aktif di demo")
    elif env == "prod":
        if is_sqlite(settings):
            problems.append(
                "produksi berjalan di server: basis data wajib PostgreSQL (SQLite hanya untuk proses lokal)"
            )
        if db is not None and (db.endswith("_dev") or db.endswith("_demo")):
            problems.append(f"basis data '{db}' milik dev/demo tidak boleh dipakai produksi")
        if _opt(settings, "model_backend") in {"replay", "online"}:
            problems.append(f"backend model '{_opt(settings, 'model_backend')}' dilarang di produksi")
        if _opt(settings, "model_api_key"):
            problems.append("kunci API model online dilarang di produksi")
        if _opt(settings, "auth_disabled"):
            problems.append("login wajib aktif di produksi")

    if env != "dev" and _opt(settings, "dev_endpoints"):
        problems.append("endpoint dev hanya boleh di ASTRIA_ENV=dev")
    return problems


def enforce(settings: AstriaSettings) -> None:
    problems = violations(settings)
    if problems:
        raise EnvironmentGuardError(settings.env, problems)
