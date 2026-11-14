"""Uji unit `Consumer` dengan DB dan NATS palsu: jalur sukses, galat sementara, `PermanentError`, dan kegagalan DLQ.

Tanpa PostgreSQL dan NATS; `FakeDb` meniru transaksi (penanda `processed_events` hanya tersimpan bila commit),
`FakeMsg` meniru pesan JetStream (nomor pengiriman, `ack`/`nak`). Uji end to end ada di test_integration.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import orjson
import pytest
from sqlalchemy.dialects import postgresql

from astria_common.consumer import Consumer, PermanentError
from astria_common.events import new_event
from astria_contracts import example

pytestmark = pytest.mark.anyio

BACKOFF = (1.0, 5.0, 30.0)


class KartuTidakValid(PermanentError, ValueError):
    """Bentuk yang dipakai handler layanan: galat domain yang sekaligus ditandai permanen."""


class FakeResult:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def first(self) -> tuple | None:
        return self._row


class FakeConn:
    def __init__(self, db: FakeDb) -> None:
        self._db = db
        self.pending: set[str] = set()

    async def execute(self, stmt) -> FakeResult:
        event_id = stmt.compile(dialect=postgresql.dialect()).params["event_id"]
        if event_id in self._db.processed or event_id in self.pending:
            return FakeResult(None)
        self.pending.add(event_id)
        return FakeResult((event_id,))


class FakeDb:
    """`processed` berisi event_id yang transaksinya commit; transaksi yang melempar galat dibatalkan."""

    def __init__(self) -> None:
        self.processed: set[str] = set()
        self.engine = SimpleNamespace(begin=self._begin)

    @asynccontextmanager
    async def _begin(self):
        conn = FakeConn(self)
        yield conn
        self.processed |= conn.pending  # hanya tercapai tanpa galat (commit)


class FakeBus:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.published: list[dict] = []
        self.fail_publish = False

    def subject(self, event_type: str) -> str:
        return f"t.{event_type}"

    def dlq_subject(self, consumer: str) -> str:
        return f"t.dlq.{consumer}"

    async def publish_raw(self, subject: str, payload: bytes, msg_id: str, event_type: str = "raw") -> None:
        if self.fail_publish:
            raise ConnectionError("NATS terputus")
        self._calls.append("publish")
        self.published.append({"subject": subject, "record": orjson.loads(payload), "msg_id": msg_id})


class FakeMsg:
    def __init__(self, data: bytes, calls: list[str], *, delivered: int = 1, stream_seq: int = 42) -> None:
        self.data = data
        self.subject = "t.evidence.ingested"
        self.headers = None
        self.metadata = SimpleNamespace(num_delivered=delivered, sequence=SimpleNamespace(stream=stream_seq))
        self._calls = calls
        self.acks = 0
        self.naks: list[float | None] = []
        self.fail_ack = False

    async def ack(self) -> None:
        if self.fail_ack:
            raise ConnectionError("ack gagal")
        self._calls.append("ack")
        self.acks += 1

    async def nak(self, delay: float | None = None) -> None:
        self._calls.append("nak")
        self.naks.append(delay)


class Harness:
    def __init__(self, handler, *, max_deliver: int = 5) -> None:
        self.calls: list[str] = []
        self.db = FakeDb()
        self.bus = FakeBus(self.calls)
        self.event = new_event("evidence.ingested", example("evidence.ingested")["data"])
        self.consumer = Consumer(
            self.bus,  # type: ignore[arg-type]
            self.db,  # type: ignore[arg-type]
            durable="uji",
            event_type="evidence.ingested",
            handler=handler,
            max_deliver=max_deliver,
            backoff=BACKOFF,
        )

    def msg(self, *, delivered: int = 1) -> FakeMsg:
        payload = orjson.dumps(self.event.model_dump(mode="json", by_alias=True))
        return FakeMsg(payload, self.calls, delivered=delivered)

    async def deliver(self, msg: FakeMsg) -> None:
        await self.consumer._handle(msg)  # type: ignore[arg-type]


def raising(exc: Exception):
    seen: list[str] = []

    async def handler(_conn, envelope, _data) -> None:
        seen.append(envelope.id)
        raise exc

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


async def test_success_acks_and_marks_processed() -> None:
    seen: list[str] = []

    async def handler(_conn, envelope, _data) -> None:
        seen.append(envelope.id)

    h = Harness(handler)
    msg = h.msg()
    await h.deliver(msg)

    assert seen == [h.event.id]
    assert h.db.processed == {h.event.id}
    assert msg.acks == 1 and msg.naks == []
    assert h.bus.published == []
    assert (h.consumer.processed, h.consumer.duplicates, h.consumer.dead_lettered) == (1, 0, 0)


async def test_duplicate_is_acked_without_calling_handler() -> None:
    seen: list[str] = []

    async def handler(_conn, envelope, _data) -> None:
        seen.append(envelope.id)

    h = Harness(handler)
    await h.deliver(h.msg())
    again = h.msg(delivered=2)
    await h.deliver(again)

    assert seen == [h.event.id]  # handler hanya sekali
    assert again.acks == 1 and again.naks == []
    assert (h.consumer.processed, h.consumer.duplicates) == (1, 1)


@pytest.mark.parametrize("exc_type", [PermanentError, KartuTidakValid])
async def test_permanent_error_goes_straight_to_dlq(exc_type) -> None:
    handler = raising(exc_type("kode.band tidak dikenal: 'X'"))
    h = Harness(handler, max_deliver=5)
    msg = h.msg(delivered=1)  # pengiriman pertama: jalur coba ulang biasa akan nak
    await h.deliver(msg)

    assert handler.seen == [h.event.id]
    assert msg.naks == []  # tidak masuk jalur backoff
    assert msg.acks == 1
    assert h.calls == ["publish", "ack"]  # DLQ dulu, baru ack pesan asli
    [dlq] = h.bus.published
    assert dlq["subject"] == "t.dlq.uji"
    assert dlq["msg_id"] == "dlq-uji-42"
    record = dlq["record"]
    assert record["reason"] == f"permanen:{exc_type.__name__}"
    assert record["detail"] == "kode.band tidak dikenal: 'X'"
    assert record["event_id"] == h.event.id
    assert record["trace_id"] == h.event.trace_id
    assert record["stream_seq"] == 42
    original = orjson.loads(record["payload"])
    assert original == h.event.model_dump(mode="json", by_alias=True)  # amplop utuh, termasuk correlation dan trace_id
    assert h.db.processed == set()  # transaksi dibatalkan: peristiwa tidak ditandai berhasil
    assert (h.consumer.processed, h.consumer.dead_lettered) == (0, 1)


async def test_permanent_detail_is_truncated() -> None:
    h = Harness(raising(PermanentError("x" * 2000)))
    await h.deliver(h.msg())
    assert len(h.bus.published[0]["record"]["detail"]) == 500


@pytest.mark.parametrize("delivered", [1, 2, 4])
async def test_ordinary_error_retries_with_backoff(delivered: int) -> None:
    handler = raising(ValueError("data aneh"))  # ValueError biasa bukan permanen
    h = Harness(handler, max_deliver=5)
    msg = h.msg(delivered=delivered)
    await h.deliver(msg)

    assert msg.naks == [BACKOFF[min(delivered, len(BACKOFF)) - 1]]
    assert msg.acks == 0
    assert h.bus.published == []
    assert h.db.processed == set()
    assert h.consumer.dead_lettered == 0


async def test_ordinary_error_dead_letters_at_max_deliver() -> None:
    h = Harness(raising(RuntimeError("gagal")), max_deliver=3)
    msg = h.msg(delivered=3)
    await h.deliver(msg)

    assert h.calls == ["publish", "ack"]
    assert h.bus.published[0]["record"]["reason"] == "gagal_berulang:RuntimeError"
    assert "detail" not in h.bus.published[0]["record"]
    assert msg.naks == []
    assert h.consumer.dead_lettered == 1


async def test_dlq_publish_failure_does_not_ack_permanent_error() -> None:
    h = Harness(raising(PermanentError("tidak valid")), max_deliver=5)
    h.bus.fail_publish = True
    msg = h.msg(delivered=1)
    await h.deliver(msg)  # tidak boleh melempar

    assert msg.acks == 0  # pesan asli tidak hilang
    assert msg.naks == [BACKOFF[0]]  # dikirim ulang untuk mencoba DLQ lagi
    assert h.consumer.dead_lettered == 0
    assert h.db.processed == set()
    assert h.consumer._last_error == "ConnectionError"


async def test_dlq_publish_failure_at_last_delivery_leaves_message_unacked() -> None:
    h = Harness(raising(RuntimeError("gagal")), max_deliver=2)
    h.bus.fail_publish = True
    msg = h.msg(delivered=2)
    await h.deliver(msg)

    assert msg.acks == 0 and msg.naks == []  # tidak di-ack: tetap di stream untuk diputar ulang
    assert h.consumer.dead_lettered == 0


async def test_undecodable_message_survives_dlq_failure() -> None:
    h = Harness(raising(AssertionError("tidak boleh dipanggil")))
    h.bus.fail_publish = True
    msg = FakeMsg(b'{"bukan": "amplop"}', h.calls, delivered=1)
    await h.deliver(msg)

    assert msg.acks == 0 and msg.naks == [BACKOFF[0]]


async def test_permanent_error_redelivered_after_failed_ack_is_consistent() -> None:
    """DLQ terbit tetapi ack pesan asli gagal -> kirim ulang: DLQ diterbitkan lagi dengan msg_id yang sama
    (disaring JetStream), pesan akhirnya di-ack, dan event tidak pernah ditandai berhasil."""
    handler = raising(PermanentError("tidak valid"))
    h = Harness(handler, max_deliver=5)

    first = h.msg(delivered=1)
    first.fail_ack = True
    await h.deliver(first)
    assert first.acks == 0 and first.naks == [BACKOFF[0]]
    assert h.consumer.dead_lettered == 0

    second = h.msg(delivered=2)
    await h.deliver(second)
    assert second.acks == 1 and second.naks == []
    assert [p["msg_id"] for p in h.bus.published] == ["dlq-uji-42", "dlq-uji-42"]
    assert handler.seen == [h.event.id, h.event.id]
    assert h.db.processed == set()
    assert h.consumer.dead_lettered == 1
