"""Token layanan JWT EdDSA (Ed25519) untuk panggilan di dalam Core.

- identity: `SigningKey` + `issue_service_token` menerbitkan token umur 300 detik dengan `aud` wajib.
- Pemanggil: `ServiceTokenClient` meminta token ke identity (permintaan ber-HMAC dengan kunci layanan
  awal) dan menyimpannya sampai hampir kedaluwarsa.
- Penerima: `ServiceTokenVerifier` memverifikasi offline dengan kunci publik dari `GET /internal/v1/keys`
  (di-cache), dan dependensi FastAPI `require_service` memeriksa audiens serta pemanggil yang diizinkan.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import orjson
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Request

from astria_common.errors import AstriaError
from astria_common.hmac_auth import signed_headers
from astria_common.http import make_client

ISSUER = "astria-identity"
TOKEN_TTL_SECONDS = 300
LEEWAY_SECONDS = 30
JWKS_TTL_SECONDS = 300
KEYS_PATH = "/internal/v1/keys"
TOKENS_PATH = "/internal/v1/service-tokens"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_key: Ed25519PrivateKey

    def public_jwk(self) -> dict[str, str]:
        raw = self.private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw), "kid": self.kid, "alg": "EdDSA", "use": "sig"}


def load_or_create_signing_key(path: Path) -> SigningKey:
    """Muat kunci Ed25519 (PEM PKCS8) dari volume identity, atau buat baru dengan izin 0600."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("kunci penandatanganan bukan Ed25519")
    else:
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = "idn-" + _b64url(raw)[:12]
    return SigningKey(kid=kid, private_key=key)


def issue_service_token(
    key: SigningKey,
    *,
    subject: str,
    audience: str,
    act: dict[str, Any] | None = None,
    ttl: int = TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, datetime]:
    issued = int(now if now is not None else time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl,
        "jti": str(uuid.uuid4()),
    }
    if act:
        claims["act"] = act
    token = jwt.encode(claims, key.private_key, algorithm="EdDSA", headers={"kid": key.kid})
    return token, datetime.fromtimestamp(issued + ttl, UTC)


@dataclass(frozen=True)
class ServicePrincipal:
    subject: str
    audience: str
    act: dict[str, Any] | None
    claims: dict[str, Any] = field(repr=False)

    @property
    def service(self) -> str:
        return self.subject.removeprefix("svc:")


class JwksSource:
    """Kunci publik identity, di-cache; diambil ulang bila `kid` tidak dikenal (dibatasi tiap 10 detik)."""

    def __init__(self, identity_url: str, *, fetch: Callable[[], Any] | None = None) -> None:
        self._identity_url = identity_url
        self._fetch_override = fetch
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = -float(JWKS_TTL_SECONDS)
        self._lock = asyncio.Lock()

    @staticmethod
    def parse(jwks: dict[str, Any]) -> dict[str, Ed25519PublicKey]:
        keys = {}
        for jwk in jwks.get("keys", []):
            if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519":
                keys[jwk["kid"]] = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
        return keys

    async def _fetch(self) -> dict[str, Any]:
        if self._fetch_override is not None:
            result = self._fetch_override()
            return await result if asyncio.iscoroutine(result) else result
        async with make_client(self._identity_url) as client:
            resp = await client.get(KEYS_PATH)
            resp.raise_for_status()
            return resp.json()

    async def get(self, kid: str) -> Ed25519PublicKey | None:
        if kid in self._keys and time.monotonic() - self._fetched_at < JWKS_TTL_SECONDS:
            return self._keys[kid]
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if age >= JWKS_TTL_SECONDS or (kid not in self._keys and age >= 10):
                self._keys = self.parse(await self._fetch())
                self._fetched_at = time.monotonic()
        return self._keys.get(kid)


class ServiceTokenVerifier:
    def __init__(self, jwks: JwksSource) -> None:
        self._jwks = jwks

    async def verify(self, token: str, audience: str) -> ServicePrincipal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AstriaError("TOKEN_TIDAK_VALID", "token layanan tidak valid", 401) from exc
        if header.get("alg") != "EdDSA" or not header.get("kid"):
            raise AstriaError("TOKEN_TIDAK_VALID", "token layanan tidak valid", 401)
        try:
            key = await self._jwks.get(header["kid"])
        except httpx.HTTPError as exc:
            raise AstriaError("IDENTITY_TIDAK_TERSEDIA", "kunci publik identity tidak tersedia", 503) from exc
        if key is None:
            raise AstriaError("TOKEN_TIDAK_VALID", "kunci token tidak dikenal", 401)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["EdDSA"],
                audience=audience,
                issuer=ISSUER,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.InvalidAudienceError as exc:
            raise AstriaError("AUDIENS_SALAH", "token untuk layanan lain", 403) from exc
        except jwt.PyJWTError as exc:
            raise AstriaError("TOKEN_TIDAK_VALID", "token layanan tidak valid atau kedaluwarsa", 401) from exc
        return ServicePrincipal(subject=claims["sub"], audience=audience, act=claims.get("act"), claims=claims)


def require_service(audience: str, allowed_callers: Iterable[str] | None = None) -> Callable[..., Any]:
    """Dependensi FastAPI: wajib token layanan untuk `audience`; opsional batasi pemanggil (mis. {'svc:api'})."""
    allowed = set(allowed_callers) if allowed_callers is not None else None

    async def dependency(request: Request) -> ServicePrincipal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AstriaError("TOKEN_WAJIB", "token layanan wajib", 401)
        verifier: ServiceTokenVerifier = request.app.state.token_verifier
        principal = await verifier.verify(token, audience)
        if allowed is not None and principal.subject not in allowed:
            raise AstriaError("PEMANGGIL_DITOLAK", "layanan ini tidak boleh memanggil endpoint ini", 403)
        return principal

    return dependency


class ServiceTokenClient:
    """Meminta dan menyimpan token layanan per (audiens, act). Diperbarui 60 detik sebelum kedaluwarsa."""

    def __init__(self, service: str, key: bytes, identity_url: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._service = service
        self._key = key
        self._http = http or make_client(identity_url)
        self._cache: dict[tuple[str, bytes], tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def token(self, audience: str, act: dict[str, Any] | None = None) -> str:
        cache_key = (audience, orjson.dumps(act, option=orjson.OPT_SORT_KEYS) if act else b"")
        cached = self._cache.get(cache_key)
        if cached and cached[1] - time.time() > 60:
            return cached[0]
        async with self._lock:
            body = orjson.dumps({"audience": audience} | ({"act": act} if act else {}))
            headers = signed_headers(self._service, self._key, "POST", TOKENS_PATH, body)
            resp = await self._http.post(
                TOKENS_PATH, content=body, headers=headers | {"Content-Type": "application/json"}
            )
            if resp.status_code != 201:
                raise AstriaError("TOKEN_DITOLAK", f"identity menolak permintaan token ({resp.status_code})", 503)
            payload = resp.json()
            expires = datetime.fromisoformat(payload["expires_at"]).timestamp()
            self._cache[cache_key] = (payload["token"], expires)
            return payload["token"]

    async def auth_headers(self, audience: str, act: dict[str, Any] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token(audience, act)}"}

    async def aclose(self) -> None:
        await self._http.aclose()
