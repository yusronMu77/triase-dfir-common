"""Pagar `ASTRIA_ENV`: layanan menolak mulai bila konfigurasinya bertentangan dengan lingkungannya.

Aturan dari note arsitektur tiga lingkungan (bagian 2):

| ASTRIA_ENV | Wajib | Dilarang |
|---|---|---|
| dev  | nama basis data `*_dev` | basis data `*_demo` atau tanpa akhiran lingkungan |
| demo | nama basis data `*_demo` | basis data permanen (`*_dev` atau tanpa akhiran), Collector |
| prod | login aktif | backend replay, kunci API model online, basis data `*_dev`/`*_demo`, endpoint dev |
| test | - | - (hanya untuk uji otomatis) |

Field khusus layanan diperiksa bila ada di kelas settings turunan: `model_backend`, `model_api_key`,
`auth_disabled`, `dev_endpoints`, `collector_enabled`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from astria_common.settings import AstriaSettings


class EnvironmentGuardError(RuntimeError):
    """Konfigurasi bertentangan dengan ASTRIA_ENV; proses harus berhenti sebelum membuka port."""

    def __init__(self, env: str, problems: list[str]) -> None:
        self.env = env
        self.problems = problems
        super().__init__(f"ASTRIA_ENV={env} menolak mulai: " + "; ".join(problems))


def database_name(settings: AstriaSettings) -> str | None:
    if settings.database_url is None:
        return None
    path = urlsplit(settings.database_url.get_secret_value()).path
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
