"""Outbox transaksional dan relay ke JetStream.

Penerbit menulis perubahan domain dan baris outbox dalam satu transaksi (`enqueue`). `OutboxRelay`
mengambil baris yang belum terbit dengan `FOR UPDATE SKIP LOCKED` (aman untuk beberapa replika),
menerbitkan dengan `Nats-Msg-Id` = id event, lalu menandai `published_at`. Pengiriman minimal sekali;
duplikat disaring JetStream (jendela duplikat) dan tabel `processed_events` di konsumen.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Index, MetaData, Table, Text, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_common.component import Check
from astria_common.db import Database
from astria_common.events import EventBus, subject_for
from astria_common.log import get_logger
from astria_common.metrics import OUTBOX_BACKLOG, OUTBOX_OLDEST_SECONDS
from astria_contracts import Envelope

log = get_logger(__name__)

metadata = MetaData()

outbox = Table(
    "outbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("subject", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("envelope", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("published_at", DateTime(timezone=True), nullable=True),
)
Index("outbox_belum_terbit", outbox.c.created_at, postgresql_where=outbox.c.published_at.is_(None))

processed_events = Table(
    "processed_events",
    metadata,
    Column("consumer", Text, primary_key=True),
    Column("event_id", Text, primary_key=True),
    Column("processed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

STALE_AFTER_SECONDS = 300


async def enqueue(conn: AsyncConnection, envelope: Envelope) -> None:
    """Tulis event ke outbox di transaksi `conn` milik pemanggil (bersama perubahan domain)."""
    await conn.execute(
        outbox.insert().values(
            id=envelope.id,
            subject=subject_for(envelope.type),
            event_type=envelope.type,
            envelope=envelope.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    )


class OutboxRelay:
    """Komponen relay. `degraded` bila ada baris lebih tua dari 5 menit yang belum terbit."""

    name = "outbox"
    required = False

    def __init__(self, db: Database, bus: EventBus, *, batch_size: int = 100, interval: float = 0.5) -> None:
        self._db = db
        self._bus = bus
        self._batch_size = batch_size
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def _run(self) -> None:
        await self._db.wait_ready()
        await self._bus.wait_connected()
        while True:
            try:
                sent = await self.relay_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - dicoba lagi pada putaran berikutnya
                self._last_error = type(exc).__name__
                log.warning("outbox_relay_gagal", error=self._last_error)
                sent = 0
            if sent < self._batch_size:
                await asyncio.sleep(self._interval)

    async def relay_once(self) -> int:
        sent = 0
        async with self._db.engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(outbox.c.id, outbox.c.envelope)
                    .where(outbox.c.published_at.is_(None))
                    .order_by(outbox.c.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                envelope = Envelope.model_validate(row.envelope)
                await self._bus.publish(envelope)
                await conn.execute(update(outbox).where(outbox.c.id == row.id).values(published_at=datetime.now(UTC)))
                sent += 1
        return sent

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        if not self._db.is_ready:
            return Check("down", "menunggu basis data")
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(func.count(), func.min(outbox.c.created_at)).where(outbox.c.published_at.is_(None))
                )
            ).one()
        backlog, oldest = row[0], row[1]
        age = (datetime.now(UTC) - oldest).total_seconds() if oldest else 0.0
        OUTBOX_BACKLOG.set(backlog)
        OUTBOX_OLDEST_SECONDS.set(age)
        detail = f"backlog={backlog} oldest_s={int(age)}" + (f" error={self._last_error}" if self._last_error else "")
        return Check("degraded" if age > STALE_AFTER_SECONDS or self._last_error else "ok", detail)
