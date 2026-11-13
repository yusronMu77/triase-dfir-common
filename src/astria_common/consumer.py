"""Konsumen JetStream idempoten dengan coba ulang berjeda dan antrean surat mati.

Setiap pesan diproses dalam satu transaksi: baris `processed_events (consumer, event_id)` disisipkan dulu;
bila sudah ada, pesan adalah duplikat dan hanya di-ack. Efek samping handler ditulis di transaksi yang sama,
jadi kegagalan membatalkan keduanya dan pesan dikirim ulang. Pesan yang tidak bisa diurai, atau gagal
`max_deliver` kali, diterbitkan ke `astria.dlq.<konsumen>` lalu di-ack; jumlahnya terlihat di /health.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import orjson
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_common.component import Check
from astria_common.db import Database
from astria_common.events import EventBus, UnknownEventType, decode
from astria_common.log import get_logger
from astria_common.metrics import EVENT_HANDLE_SECONDS, EVENTS_CONSUMED
from astria_common.outbox import processed_events
from astria_common.tracing import bind_trace, record_span_error, reset_trace, trace_span
from astria_contracts import Envelope

log = get_logger(__name__)

Handler = Callable[[AsyncConnection, Envelope, BaseModel], Awaitable[None]]
DEFAULT_BACKOFF = (1.0, 5.0, 30.0, 120.0)


class Consumer:
    """Komponen konsumen pull durable untuk satu jenis event."""

    required = True

    def __init__(
        self,
        bus: EventBus,
        db: Database,
        *,
        durable: str,
        event_type: str,
        handler: Handler,
        max_deliver: int = 5,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        ack_wait: float = 30.0,
    ) -> None:
        self.name = f"consumer:{durable}"
        self._bus = bus
        self._db = db
        self._durable = durable
        self._event_type = event_type
        self._subject = bus.subject(event_type)
        self._handler = handler
        self._max_deliver = max_deliver
        self._backoff = list(backoff)
        self._ack_wait = ack_wait
        self._task: asyncio.Task[None] | None = None
        self._subscribed = asyncio.Event()
        self.processed = 0
        self.duplicates = 0
        self.dead_lettered = 0
        self._last_error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"consumer-{self._durable}")

    async def wait_subscribed(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._subscribed.wait(), timeout=timeout)

    async def _run(self) -> None:
        await self._db.wait_ready()
        await self._bus.wait_connected()
        while True:
            try:
                sub = await self._bus.js.pull_subscribe(
                    self._subject,
                    durable=self._durable,
                    config=ConsumerConfig(
                        ack_policy=AckPolicy.EXPLICIT,
                        deliver_policy=DeliverPolicy.ALL,
                        max_deliver=self._max_deliver,
                        ack_wait=self._ack_wait,
                    ),
                )
                break
            except Exception as exc:  # noqa: BLE001
                self._last_error = type(exc).__name__
                log.warning("konsumen_gagal_berlangganan", consumer=self._durable, error=self._last_error)
                await asyncio.sleep(2)
        self._subscribed.set()
        log.info("konsumen_siap", consumer=self._durable, subject=self._subject)
        while True:
            try:
                msgs = await sub.fetch(10, timeout=1)
            except NatsTimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001 - mis. terputus sementara; NATS menyambung ulang
                self._last_error = type(exc).__name__
                await asyncio.sleep(1)
                continue
            for msg in msgs:
                await self._handle(msg)

    async def _handle(self, msg: Msg) -> None:
        try:
            envelope, data = decode(msg.data)
        except (ValidationError, UnknownEventType, ValueError) as exc:
            await self._dead_letter(msg, f"tidak_valid:{type(exc).__name__}")
            return

        attributes = {
            "messaging.system": "nats",
            "messaging.operation.type": "process",
            "messaging.destination.name": msg.subject,
            "messaging.consumer.group.name": self._durable,
            "messaging.message.id": envelope.id,
            "astria.event.type": envelope.type,
        }
        started = time.perf_counter()
        token = bind_trace(envelope.trace_id, **envelope.correlation.model_dump(exclude_none=True))
        try:
            with trace_span(
                f"process {envelope.type}",
                kind="consumer",
                trace_id=envelope.trace_id,
                carrier=msg.headers,
                attributes=attributes,
            ):
                await self._process(msg, envelope, data)
        finally:
            EVENT_HANDLE_SECONDS.labels(self._durable).observe(time.perf_counter() - started)
            reset_trace(token)

    async def _process(self, msg: Msg, envelope: Envelope, data: BaseModel) -> None:
        try:
            async with self._db.engine.begin() as conn:
                inserted = await conn.execute(
                    insert(processed_events)
                    .values(consumer=self._durable, event_id=envelope.id)
                    .on_conflict_do_nothing()
                    .returning(processed_events.c.event_id)
                )
                if inserted.first() is None:
                    self.duplicates += 1
                    EVENTS_CONSUMED.labels(self._durable, "duplicate").inc()
                    log.info("event_duplikat", event_id=envelope.id, type=envelope.type)
                else:
                    await self._handler(conn, envelope, data)
                    self.processed += 1
                    EVENTS_CONSUMED.labels(self._durable, "ok").inc()
                    log.info("event_diproses", event_id=envelope.id, type=envelope.type)
            await msg.ack()
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - kegagalan handler: coba ulang lalu surat mati
            self._last_error = type(exc).__name__
            record_span_error(exc)
            delivered = msg.metadata.num_delivered or 1
            log.warning("event_gagal", event_id=envelope.id, attempt=delivered, error=self._last_error)
            EVENTS_CONSUMED.labels(self._durable, "retry").inc()
            if delivered >= self._max_deliver:
                await self._dead_letter(msg, f"gagal_berulang:{self._last_error}")
            else:
                await msg.nak(delay=self._backoff[min(delivered, len(self._backoff)) - 1])

    async def _dead_letter(self, msg: Msg, reason: str) -> None:
        meta = msg.metadata
        record = {
            "consumer": self._durable,
            "reason": reason,
            "subject": msg.subject,
            "stream_seq": meta.sequence.stream if meta and meta.sequence else None,
            "payload": msg.data.decode("utf-8", errors="replace"),
        }
        msg_id = f"dlq-{self._durable}-{record['stream_seq']}"
        await self._bus.publish_raw(self._bus.dlq_subject(self._durable), orjson.dumps(record), msg_id, "dlq")
        await msg.ack()
        self.dead_lettered += 1
        EVENTS_CONSUMED.labels(self._durable, "dead_letter").inc()
        log.error("event_surat_mati", consumer=self._durable, reason=reason)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        detail = f"processed={self.processed} duplicates={self.duplicates} dlq={self.dead_lettered}"
        if not self._subscribed.is_set():
            return Check("down", f"belum berlangganan ({self._last_error or 'menunggu'})")
        if self.dead_lettered:
            return Check("degraded", detail)
        return Check("ok", detail)
