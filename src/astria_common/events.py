"""Amplop event dan bus NATS JetStream.

- `new_event` membangun amplop yang sudah divalidasi terhadap model kontrak (astria_contracts).
- `EventBus` menyambung ke NATS di latar, memastikan aliran `ASTRIA` ada, dan menerbitkan dengan
  `Nats-Msg-Id` = id event (dedupe JetStream dalam jendela duplikat).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import PubAck, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BadRequestError, NotFoundError
from pydantic import BaseModel

from astria_common.component import Check
from astria_common.log import get_logger
from astria_common.metrics import EVENTS_PUBLISHED
from astria_common.tracing import current_trace_id, new_trace_id
from astria_contracts import DUPLICATE_WINDOW_SECONDS, EVENTS, STREAM_NAME, STREAM_SUBJECTS, Correlation, Envelope

log = get_logger(__name__)

STREAM_MAX_AGE_SECONDS = 7 * 24 * 3600


class UnknownEventType(KeyError):
    pass


def subject_for(event_type: str) -> str:
    try:
        return EVENTS[event_type].subject
    except KeyError as exc:
        raise UnknownEventType(event_type) from exc


def dlq_subject(consumer: str) -> str:
    return f"astria.dlq.{consumer}"


def new_event(
    event_type: str,
    data: BaseModel | Mapping[str, Any],
    *,
    correlation: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> Envelope:
    """Bangun amplop; `data` divalidasi dengan model kontrak jenis ini (gagal = ValidationError)."""
    spec = EVENTS.get(event_type)
    if spec is None:
        raise UnknownEventType(event_type)
    raw = data.model_dump(mode="json", by_alias=True) if isinstance(data, BaseModel) else dict(data)
    validated = spec.model.model_validate(raw)
    return Envelope(
        id=event_id or str(uuid.uuid4()),
        type=event_type,
        version=spec.version,
        occurred_at=occurred_at or datetime.now(UTC),
        trace_id=trace_id or current_trace_id() or new_trace_id(),
        correlation=Correlation.model_validate(dict(correlation or {})),
        data=validated.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def encode(envelope: Envelope) -> bytes:
    return envelope.model_dump_json(by_alias=True, exclude_none=True).encode()


def decode(payload: bytes) -> tuple[Envelope, BaseModel]:
    """Urai amplop dan validasi `data` dengan model kontrak. Melempar ValidationError atau UnknownEventType."""
    envelope = Envelope.model_validate_json(payload)
    spec = EVENTS.get(envelope.type)
    if spec is None:
        raise UnknownEventType(envelope.type)
    return envelope, spec.model.model_validate(envelope.data)


class EventBus:
    """Komponen NATS JetStream. Menyambung ulang tanpa batas; /ready `down` selama terputus."""

    name = "nats"
    required = True

    def __init__(self, url: str, *, client_name: str, stream: str = STREAM_NAME, prefix: str = "astria") -> None:
        self._url = url
        self._client_name = client_name
        self.stream = stream
        self.prefix = prefix
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    def subject(self, event_type: str) -> str:
        """Subjek NATS untuk jenis event (prefiks bisa diganti, mis. untuk uji terisolasi)."""
        return self.prefix + subject_for(event_type).removeprefix("astria")

    def dlq_subject(self, consumer: str) -> str:
        return f"{self.prefix}.dlq.{consumer}"

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("EventBus belum tersambung")
        return self._js

    async def start(self) -> None:
        self._task = asyncio.create_task(self._connect_loop(), name="nats-connect")

    async def _connect_loop(self) -> None:
        delay = 0.5
        while True:
            try:
                self._nc = await nats.connect(
                    self._url,
                    name=self._client_name,
                    connect_timeout=3,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                    drain_timeout=3,
                )
                self._js = self._nc.jetstream()
                await self.ensure_stream()
                self._connected.set()
                self._last_error = None
                log.info("nats_tersambung", stream=self.stream)
                return
            except Exception as exc:  # noqa: BLE001 - dicoba ulang; status terlihat di /ready
                self._last_error = type(exc).__name__
                log.warning("nats_belum_siap", error=self._last_error, retry_in=delay)
                if self._nc is not None and not self._nc.is_closed:
                    await self._nc.close()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)

    async def ensure_stream(self) -> None:
        try:
            await self.js.stream_info(self.stream)
            return
        except NotFoundError:
            pass
        subjects = list(STREAM_SUBJECTS) if self.prefix == "astria" else [f"{self.prefix}.>"]
        config = StreamConfig(
            name=self.stream,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=STREAM_MAX_AGE_SECONDS,
            duplicate_window=DUPLICATE_WINDOW_SECONDS,
            num_replicas=1,
        )
        try:
            await self.js.add_stream(config)
            log.info("stream_dibuat", stream=self.stream)
        except BadRequestError:
            # Layanan lain membuatnya bersamaan; cukup pastikan sekarang ada.
            await self.js.stream_info(self.stream)

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def publish(self, envelope: Envelope, *, subject: str | None = None) -> PubAck:
        return await self.publish_raw(
            subject or self.subject(envelope.type), encode(envelope), envelope.id, envelope.type
        )

    async def publish_raw(self, subject: str, payload: bytes, msg_id: str, event_type: str = "raw") -> PubAck:
        try:
            ack = await self.js.publish(subject, payload, headers={"Nats-Msg-Id": msg_id}, timeout=5)
        except Exception:
            EVENTS_PUBLISHED.labels(event_type, "error").inc()
            raise
        EVENTS_PUBLISHED.labels(event_type, "duplicate" if ack.duplicate else "ok").inc()
        return ack

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._nc is not None and not self._nc.is_closed:
            await self._nc.drain()

    async def check(self) -> Check:
        if self._nc is None or not self._connected.is_set():
            return Check("down", f"menyambung ({self._last_error or 'mulai'})")
        if not self._nc.is_connected:
            return Check("down", "terputus, menyambung ulang")
        return Check("ok")
