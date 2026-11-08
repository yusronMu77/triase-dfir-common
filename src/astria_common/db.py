"""PostgreSQL per layanan: engine async (psycopg 3), skema sendiri, dan pembuatan tabel bertahap.

Tahap 0 membuat tabel dengan `metadata.create_all` dan SQL tambahan (mis. pemicu audit) saat start.
Migrasi Alembic per layanan menggantikannya mulai Tahap 1.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from pydantic import SecretStr
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from astria_common.component import Check
from astria_common.log import get_logger

log = get_logger(__name__)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class Database:
    """Komponen basis data. Menyambung dan menyiapkan skema di latar; /ready `down` sampai siap."""

    name = "postgres"
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

    async def start(self) -> None:
        connect_args = {"options": f"-csearch_path={self._schema}"} if self._schema else {}
        self._engine = create_async_engine(
            self._url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=self._pool_size,
            connect_args=connect_args,
        )
        self._task = asyncio.create_task(self._prepare(), name="db-prepare")

    async def _prepare(self) -> None:
        delay = 0.5
        while True:
            try:
                async with self.engine.begin() as conn:
                    if self._schema:
                        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                    for md in self._metadata:
                        await conn.run_sync(md.create_all)
                    for statement in self._setup_sql:
                        await conn.execute(text(statement))
                self._ready.set()
                self._last_error = None
                log.info("db_siap", schema=self._schema or "public")
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
