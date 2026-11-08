"""Basis data per layanan: PostgreSQL di server, SQLite untuk proses lokal.

Keputusan pemilik (2026-11-08): layanan yang berjalan di server memakai PostgreSQL (engine async psycopg 3,
skema sendiri). Proses lokal yang butuh basis data lokal (laptop dev, uji otomatis, demo luring) memakai SQLite
(aiosqlite, mode WAL). Dialek dipilih dari skema `ASTRIA_DATABASE_URL`; `prod` menolak SQLite (envguard).

Tahap 0 membuat tabel dengan `metadata.create_all` dan SQL tambahan (mis. pemicu audit) saat start.
Migrasi Alembic per layanan menggantikannya mulai Tahap 1. `setup_sql` ditulis untuk PostgreSQL; di SQLite
berikan `setup_sql` yang sesuai atau kosongkan.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import SecretStr
from sqlalchemy import MetaData, event, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from astria_common.component import Check
from astria_common.log import get_logger

log = get_logger(__name__)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

Dialect = Literal["postgresql", "sqlite"]


def dialect_of(url: str) -> Dialect:
    """Dialek dari skema URL. Selain `postgresql*` dan `sqlite*` ditolak (tidak ada tebakan diam-diam)."""
    backend = make_url(url).get_backend_name()
    if backend == "postgresql":
        return "postgresql"
    if backend == "sqlite":
        return "sqlite"
    raise ValueError(f"dialek basis data tidak didukung: {backend!r} (pakai postgresql atau sqlite)")


def normalize_url(url: str) -> str:
    """Pastikan driver async: `postgresql://` jadi `postgresql+psycopg://`, `sqlite://` jadi `sqlite+aiosqlite://`."""
    parsed = make_url(url)
    dialect = dialect_of(url)
    if dialect == "postgresql" and parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    elif dialect == "sqlite" and parsed.drivername == "sqlite":
        parsed = parsed.set(drivername="sqlite+aiosqlite")
    return parsed.render_as_string(hide_password=False)


class Database:
    """Komponen basis data. Menyambung dan menyiapkan skema di latar; /ready `down` sampai siap."""

    required = True

    def __init__(
        self,
        url: SecretStr,
        *,
        schema: str | None = None,
        metadata: Sequence[MetaData] = (),
        setup_sql: Sequence[str] = (),
        pool_size: int = 5,
    ) -> None:
        if schema is not None and not _IDENT.match(schema):
            raise ValueError(f"nama skema tidak valid: {schema!r}")
        self.dialect: Dialect = dialect_of(url.get_secret_value())
        # Nama komponen di /ready: "postgres" (server) atau "sqlite" (lokal).
        self.name = "postgres" if self.dialect == "postgresql" else "sqlite"
        if self.dialect == "sqlite":
            schema = None  # SQLite tidak punya skema; satu berkas per layanan
        self._url = url
        self._schema = schema
        self._metadata = list(metadata)
        self._setup_sql = list(setup_sql)
        self._pool_size = pool_size
        self._engine: AsyncEngine | None = None
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database belum dimulai")
        return self._engine

    @property
    def is_ready(self) -> bool:
        return self._engine is not None and self._ready.is_set()

    def _create_engine(self) -> AsyncEngine:
        url = normalize_url(self._url.get_secret_value())
        if self.dialect == "sqlite":
            engine = create_async_engine(url, pool_pre_ping=True)

            @event.listens_for(engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.execute("PRAGMA busy_timeout=5000")
                finally:
                    cur.close()

            return engine
        connect_args = {"options": f"-csearch_path={self._schema}"} if self._schema else {}
        return create_async_engine(url, pool_pre_ping=True, pool_size=self._pool_size, connect_args=connect_args)

    async def start(self) -> None:
        self._engine = self._create_engine()
        self._task = asyncio.create_task(self._prepare(), name="db-prepare")

    async def _prepare(self) -> None:
        delay = 0.5
        while True:
            try:
                async with self.engine.begin() as conn:
                    if self._schema and self.dialect == "postgresql":
                        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                    for md in self._metadata:
                        await conn.run_sync(md.create_all)
                    for statement in self._setup_sql:
                        await conn.execute(text(statement))
                self._ready.set()
                self._last_error = None
                log.info("db_siap", dialect=self.dialect, schema=self._schema or "public")
                return
            except Exception as exc:  # noqa: BLE001 - dicoba ulang; status terlihat di /ready
                self._last_error = type(exc).__name__
                log.warning("db_belum_siap", error=self._last_error, retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)

    async def wait_ready(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._engine is not None:
            await self._engine.dispose()

    async def check(self) -> Check:
        if not self._ready.is_set():
            return Check("down", f"menyiapkan skema ({self._last_error or 'menyambung'})")
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return Check("ok")
