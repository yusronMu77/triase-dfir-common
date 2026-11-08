"""Komponen berumur sama dengan layanan (basis data, bus event, relay, konsumen).

`create_app` memulai komponen berurutan saat start dan menghentikannya terbalik saat berhenti. Status
komponen menjadi `checks` di /health; komponen `required` yang tidak `ok` membuat /ready menjawab 503.
Komponen tidak boleh membuat layanan mati karena dependensi belum siap: ia mencoba ulang di latar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Status = Literal["ok", "degraded", "down"]


@dataclass(frozen=True)
class Check:
    status: Status
    detail: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status} | ({"detail": self.detail} if self.detail else {})


@runtime_checkable
class Component(Protocol):
    name: str
    required: bool

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def check(self) -> Check: ...
