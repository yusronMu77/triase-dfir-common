"""Uji integrasi dengan PostgreSQL dan NATS sungguhan.

Butuh `ASTRIA_TEST_DATABASE_URL` (mis. postgresql+psycopg://astria_dev:...@postgres:5432/postgres) dan
`ASTRIA_TEST_NATS_URL` (mis. nats://nats:4222). Setiap uji memakai skema dan stream sendiri sehingga tidak
mengganggu stack dev yang sedang berjalan.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import orjson
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from astria_common.consumer import Consumer, PermanentError
from astria_common.db import Database
from astria_common.events import EventBus, new_event
from astria_common.outbox import OutboxRelay, enqueue, metadata, outbox, processed_events
from astria_contracts import example

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
NATS_URL = os.environ.get("ASTRIA_TEST_NATS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not (DB_URL and NATS_URL), reason="ASTRIA_TEST_DATABASE_URL/ASTRIA_TEST_NATS_URL tidak diisi"),
]


async def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("kondisi tidak tercapai dalam batas waktu")


@pytest.fixture
async def stack():
    tag = uuid.uuid4().hex[:8]
    schema = f"t_{tag}"
    db = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[metadata])
    bus = EventBus(NATS_URL or "", client_name=f"test-{tag}", stream=f"T{tag.upper()}", prefix=f"t{tag}")
    await db.start()
    await bus.start()
    await db.wait_ready(10)
    await bus.wait_connected(10)
    yield db, bus, tag
    await bus.js.delete_stream(bus.stream)
    await bus.stop()
    async with db.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await db.stop()


async def test_outbox_to_idempotent_consumer(stack) -> None:
    db, bus, tag = stack
    seen: list[str] = []

    async def handler(_conn, envelope, data) -> None:
        seen.append(envelope.id)

    consumer = Consumer(bus, db, durable=f"triage-{tag}", event_type="evidence.ingested", handler=handler)
    relay = OutboxRelay(db, bus, interval=0.1)
    await consumer.start()
    await relay.start()
    await consumer.wait_subscribed(10)

    event = new_event("evidence.ingested", example("evidence.ingested")["data"])
    async with db.engine.begin() as conn:
        await enqueue(conn, event)

    async def processed() -> bool:
        return seen == [event.id]

    await wait_until(processed)

    # Terbit ulang dengan id yang sama: JetStream menyaring dalam jendela duplikat.
    ack = await bus.publish(event)
    assert ack.duplicate

    # Lewat jendela (simulasi: stream lain tidak ada) -> konsumen tetap idempoten lewat processed_events.
    await bus.js.publish(
        bus.subject("evidence.ingested"),
        orjson.dumps(event.model_dump(mode="json", by_alias=True)),
        headers={"Nats-Msg-Id": f"{event.id}-ulang"},
    )

    async def duplicate_seen() -> bool:
        return consumer.duplicates == 1

    await wait_until(duplicate_seen)
    assert seen == [event.id]

    async with db.engine.connect() as conn:
        backlog = (await conn.execute(select(func.count()).where(outbox.c.published_at.is_(None)))).scalar_one()
        rows = (await conn.execute(select(func.count()).select_from(processed_events))).scalar_one()
    assert backlog == 0
    assert rows == 1
    assert (await consumer.check()).status == "ok"
    await relay.stop()
    await consumer.stop()


async def test_invalid_message_goes_to_dead_letter(stack) -> None:
    db, bus, tag = stack

    async def handler(_conn, envelope, data) -> None:
        raise AssertionError("tidak boleh dipanggil")

    consumer = Consumer(bus, db, durable=f"dlq-{tag}", event_type="evidence.ingested", handler=handler)
    await consumer.start()
    await consumer.wait_subscribed(10)
    await bus.js.publish(bus.subject("evidence.ingested"), b'{"bukan": "amplop"}')

    async def dead() -> bool:
        return consumer.dead_lettered == 1

    await wait_until(dead)
    info = await bus.js.stream_info(bus.stream, subjects_filter=bus.dlq_subject(f"dlq-{tag}"))
    assert info.state.messages >= 2  # pesan asli + salinan di subjek surat mati
    assert (await consumer.check()).status == "degraded"
    await consumer.stop()


async def test_failing_handler_retries_then_dead_letters(stack) -> None:
    db, bus, tag = stack
    calls = 0

    async def handler(_conn, envelope, data) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("gagal")

    consumer = Consumer(
        bus, db, durable=f"retry-{tag}", event_type="evidence.ingested", handler=handler, max_deliver=2, backoff=(0.1,)
    )
    await consumer.start()
    await consumer.wait_subscribed(10)
    await bus.publish(new_event("evidence.ingested", example("evidence.ingested")["data"]))

    async def dead() -> bool:
        return consumer.dead_lettered == 1

    await wait_until(dead, timeout=15)
    assert calls == 2
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(func.count()).select_from(processed_events))).scalar_one()
    assert rows == 0  # transaksi gagal ikut membatalkan penanda processed_events
    await consumer.stop()


async def test_permanent_error_dead_letters_without_retry(stack) -> None:
    db, bus, tag = stack
    calls = 0

    async def handler(_conn, envelope, data) -> None:
        nonlocal calls
        calls += 1
        raise PermanentError("kode.band tidak dikenal: 'X'")

    # max_deliver dan backoff bawaan: jalur coba ulang biasa baru masuk DLQ setelah sekitar 2,5 menit.
    durable = f"perm-{tag}"
    consumer = Consumer(bus, db, durable=durable, event_type="evidence.ingested", handler=handler)
    await consumer.start()
    await consumer.wait_subscribed(10)
    event = new_event("evidence.ingested", example("evidence.ingested")["data"])
    await bus.publish(event)

    async def dead() -> bool:
        return consumer.dead_lettered == 1

    await wait_until(dead, timeout=5)
    assert calls == 1
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(func.count()).select_from(processed_events))).scalar_one()
    assert rows == 0  # transaksi dibatalkan: event tidak ditandai berhasil

    dlq = await bus.js.get_last_msg(bus.stream, bus.dlq_subject(durable))
    record = orjson.loads(dlq.data)
    assert record["reason"] == "permanen:PermanentError"
    assert record["detail"] == "kode.band tidak dikenal: 'X'"
    assert record["event_id"] == event.id
    assert orjson.loads(record["payload"])["id"] == event.id

    info = await bus.js.consumer_info(bus.stream, durable)
    assert info.num_ack_pending == 0  # pesan asli di-ack setelah DLQ
    assert info.num_redelivered == 0  # tidak ada pengiriman ulang
    assert (await consumer.check()).status == "degraded"
    await consumer.stop()


async def test_trace_outbox_ke_konsumen_satu_trace(stack, otel) -> None:
    """Relay membuka span producer dengan trace id amplop; span konsumen adalah anaknya (lewat header NATS)."""
    db, bus, tag = stack
    seen: list[str | None] = []

    async def handler(_conn, envelope, data) -> None:
        from astria_common.tracing import active_span_trace_id

        seen.append(active_span_trace_id())

    consumer = Consumer(bus, db, durable=f"trace-{tag}", event_type="evidence.ingested", handler=handler)
    relay = OutboxRelay(db, bus, interval=0.1)
    await consumer.start()
    await relay.start()
    await consumer.wait_subscribed(10)

    trace_id = uuid.uuid4().hex
    event = new_event("evidence.ingested", example("evidence.ingested")["data"], trace_id=trace_id)
    async with db.engine.begin() as conn:
        await enqueue(conn, event)

    async def processed() -> bool:
        return bool(seen)

    await wait_until(processed)
    await relay.stop()
    await consumer.stop()

    assert seen == [trace_id]
    spans = {s.name: s for s in otel.get_finished_spans()}
    producer, process = spans["publish evidence.ingested"], spans["process evidence.ingested"]
    assert format(producer.context.trace_id, "032x") == trace_id == format(process.context.trace_id, "032x")
    assert process.parent is not None and process.parent.span_id == producer.context.span_id
    assert process.attributes["messaging.consumer.group.name"] == f"trace-{tag}"
