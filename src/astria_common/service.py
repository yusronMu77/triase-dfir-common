"""Rakitan komponen standar layanan: basis data, bus event, dan relay outbox sesuai konfigurasi.

Layanan tanpa `ASTRIA_DATABASE_URL` tidak punya komponen basis data; tanpa `ASTRIA_NATS_URL` tidak punya bus.
Relay outbox hanya ada bila keduanya ada. Konsumen ditambahkan layanan lewat `Runtime.add`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import MetaData

from astria_common import outbox
from astria_common.component import Component
from astria_common.db import Database
from astria_common.events import EventBus
from astria_common.outbox import OutboxRelay
from astria_common.settings import AstriaSettings


@dataclass
class Runtime:
    settings: AstriaSettings
    db: Database | None = None
    bus: EventBus | None = None
    relay: OutboxRelay | None = None
    extra: list[Component] = field(default_factory=list)

    def add(self, component: Component) -> Component:
        self.extra.append(component)
        return component

    def require_db(self) -> Database:
        if self.db is None:
            raise RuntimeError(f"{self.settings.service_name}: ASTRIA_DATABASE_URL wajib diisi")
        return self.db

    def require_bus(self) -> EventBus:
        if self.bus is None:
            raise RuntimeError(f"{self.settings.service_name}: ASTRIA_NATS_URL wajib diisi")
        return self.bus

    @property
    def components(self) -> list[Component]:
        base: list[Component] = [c for c in (self.db, self.bus, self.relay) if c is not None]
        return base + self.extra


def standard_runtime(
    settings: AstriaSettings,
    *,
    metadata: Sequence[MetaData] = (),
    setup_sql: Sequence[str] = (),
) -> Runtime:
    rt = Runtime(settings)
    if settings.database_url is not None:
        rt.db = Database(
            settings.database_url,
            schema=settings.db_schema,
            metadata=[outbox.metadata, *metadata],
            setup_sql=setup_sql,
        )
    if settings.nats_url:
        rt.bus = EventBus(settings.nats_url, client_name=f"astria-{settings.service_name}")
    if rt.db is not None and rt.bus is not None:
        rt.relay = OutboxRelay(rt.db, rt.bus)
    return rt
