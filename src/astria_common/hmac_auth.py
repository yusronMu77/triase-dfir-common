"""HMAC-SHA256 per permintaan: jalur Gateway/Collector ke Core dan penerbitan token layanan oleh identity.

String yang ditandatangani (dipisah baris baru): METHOD, PATH (dengan query), X-Astria-Ts, X-Astria-Nonce,
hex SHA-256 isi. Pemeriksa menolak cap waktu di luar jendela 300 detik dan nonce yang sudah dipakai.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

WINDOW_SECONDS = 300
H_SERVICE = "X-Astria-Service"
H_TS = "X-Astria-Ts"
H_NONCE = "X-Astria-Nonce"
H_SIG = "X-Astria-Sig"


class HmacError(Exception):
    """Permintaan ber-HMAC ditolak. `reason` aman dicatat, tidak memuat kunci."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical(method: str, path: str, ts: int, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, str(ts), nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign(key: bytes, method: str, path: str, ts: int, nonce: str, body: bytes) -> str:
    return hmac.new(key, canonical(method, path, ts, nonce, body), hashlib.sha256).hexdigest()


def signed_headers(service: str, key: bytes, method: str, path: str, body: bytes, *, now: float | None = None) -> dict:
    ts = int(now if now is not None else time.time())
    nonce = secrets.token_hex(16)
    return {
        H_SERVICE: service,
        H_TS: str(ts),
        H_NONCE: nonce,
        H_SIG: sign(key, method, path, ts, nonce, body),
    }


@dataclass
class NonceCache:
    """Nonce terpakai dalam jendela waktu (memori proses; cukup untuk satu replika)."""

    window: int = WINDOW_SECONDS
    _seen: dict[str, float] = field(default_factory=dict)

    def use(self, nonce: str, now: float) -> bool:
        """True bila nonce baru (lalu dicatat); False bila sudah dipakai."""
        if len(self._seen) > 10_000 or (self._seen and next(iter(self._seen.values())) < now):
            self._seen = {n: exp for n, exp in self._seen.items() if exp >= now}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now + self.window
        return True


def verify(
    key: bytes,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    nonces: NonceCache,
    *,
    now: float | None = None,
    window: int = WINDOW_SECONDS,
) -> None:
    now = now if now is not None else time.time()
    lowered = {k.lower(): v for k, v in headers.items()}
    try:
        ts = int(lowered[H_TS.lower()])
        nonce = lowered[H_NONCE.lower()]
        sig = lowered[H_SIG.lower()]
    except (KeyError, ValueError) as exc:
        raise HmacError("header_tidak_lengkap") from exc
    if abs(now - ts) > window:
        raise HmacError("cap_waktu_di_luar_jendela")
    if not 16 <= len(nonce) <= 64:
        raise HmacError("nonce_tidak_valid")
    expected = sign(key, method, path, ts, nonce, body)
    if not hmac.compare_digest(expected, sig):
        raise HmacError("tanda_tangan_salah")
    if not nonces.use(nonce, now):
        raise HmacError("nonce_terpakai")
