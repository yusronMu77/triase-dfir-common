"""Klien audit dua jalur (keputusan pemilik audit#2: hibrida).

- `AuditClient.record` = jalur sinkron fail-closed untuk aksi kritis: bila audit tidak bisa mencatat,
  `AuditUnavailable` dilempar dan pemanggil wajib menggagalkan aksinya.
- `audit_event` = jalur asinkron: amplop `audit.record` yang ditulis ke outbox (`outbox.enqueue`) dalam
  transaksi yang sama dengan perubahan domain.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from astria_common.auth import ServiceTokenClient
from astria_common.errors import AstriaError
from astria_common.events import new_event
from astria_common.http import make_client
from astria_contracts import Envelope
from astria_contracts.events.audit_record import AuditRecord
from astria_contracts.openapi.audit_internal import RecordReceipt

RECORDS_PATH = "/internal/v1/records"


class AuditUnavailable(AstriaError):
    def __init__(self, reason: str) -> None:
        super().__init__("AUDIT_TIDAK_TERSEDIA", f"audit tidak dapat mencatat: {reason}", 503)


class AuditClient:
    def __init__(self, tokens: ServiceTokenClient, audit_url: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._tokens = tokens
        self._http = http or make_client(audit_url)

    async def record(
        self, record: AuditRecord | Mapping[str, Any], *, act: dict[str, Any] | None = None
    ) -> RecordReceipt:
        payload = AuditRecord.model_validate(record) if not isinstance(record, AuditRecord) else record
        try:
            headers = await self._tokens.auth_headers("svc:audit", act)
            resp = await self._http.post(
                RECORDS_PATH, content=payload.model_dump_json(), headers=headers | {"Content-Type": "application/json"}
            )
        except (httpx.HTTPError, AstriaError) as exc:
            raise AuditUnavailable(type(exc).__name__) from exc
        if resp.status_code not in (200, 201):
            raise AuditUnavailable(f"status {resp.status_code}")
        return RecordReceipt.model_validate(resp.json())

    async def aclose(self) -> None:
        await self._http.aclose()


def audit_event(record: AuditRecord | Mapping[str, Any], *, correlation: Mapping[str, Any] | None = None) -> Envelope:
    return new_event("audit.record", record, correlation=correlation)
