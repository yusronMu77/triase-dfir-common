from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from astria_common.app import create_app
from astria_common.auth import (
    JwksSource,
    ServicePrincipal,
    ServiceTokenVerifier,
    issue_service_token,
    load_or_create_signing_key,
    require_service,
)
from astria_common.errors import AstriaError
from astria_common.settings import AstriaSettings


@pytest.fixture
def key(tmp_path: Path):
    return load_or_create_signing_key(tmp_path / "signing.pem")


@pytest.fixture
def verifier(key) -> ServiceTokenVerifier:
    return ServiceTokenVerifier(JwksSource("http://unused", fetch=lambda: {"keys": [key.public_jwk()]}))


def test_key_is_persisted_and_reloaded(tmp_path: Path) -> None:
    first = load_or_create_signing_key(tmp_path / "k.pem")
    second = load_or_create_signing_key(tmp_path / "k.pem")
    assert first.kid == second.kid
    assert (tmp_path / "k.pem").stat().st_mode & 0o077 == 0


@pytest.mark.anyio
async def test_issue_and_verify(key, verifier) -> None:
    act = {"sub": "usr_01ABC", "roles": ["analis"]}
    token, expires = issue_service_token(key, subject="svc:ticket", audience="svc:audit", act=act)
    principal = await verifier.verify(token, "svc:audit")
    assert principal.subject == "svc:ticket"
    assert principal.service == "ticket"
    assert principal.act == act
    assert expires.timestamp() - time.time() == pytest.approx(300, abs=5)


@pytest.mark.anyio
async def test_wrong_audience_is_403(key, verifier) -> None:
    token, _ = issue_service_token(key, subject="svc:notifier", audience="svc:ticket")
    with pytest.raises(AstriaError) as err:
        await verifier.verify(token, "svc:audit")
    assert err.value.status == 403


@pytest.mark.anyio
async def test_expired_token_is_401(key, verifier) -> None:
    token, _ = issue_service_token(key, subject="svc:ticket", audience="svc:audit", now=time.time() - 3600)
    with pytest.raises(AstriaError) as err:
        await verifier.verify(token, "svc:audit")
    assert err.value.status == 401


@pytest.mark.anyio
async def test_token_from_other_key_is_401(tmp_path: Path, verifier) -> None:
    other = load_or_create_signing_key(tmp_path / "other.pem")
    token, _ = issue_service_token(other, subject="svc:ticket", audience="svc:audit")
    with pytest.raises(AstriaError) as err:
        await verifier.verify(token, "svc:audit")
    assert err.value.status == 401


def test_require_service_dependency(key, verifier) -> None:
    router = APIRouter()

    @router.get("/internal/v1/ping")
    async def ping(p: ServicePrincipal = Depends(require_service("svc:audit", {"svc:api"}))) -> dict:  # noqa: B008
        return {"caller": p.subject}

    app = create_app(AstriaSettings(service_name="audit", env="test"), routers=[router], token_verifier=verifier)
    client = TestClient(app)

    assert client.get("/internal/v1/ping").status_code == 401
    ok, _ = issue_service_token(key, subject="svc:api", audience="svc:audit")
    resp = client.get("/internal/v1/ping", headers={"Authorization": f"Bearer {ok}"})
    assert resp.status_code == 200
    assert resp.json() == {"caller": "svc:api"}

    denied, _ = issue_service_token(key, subject="svc:notifier", audience="svc:audit")
    resp = client.get("/internal/v1/ping", headers={"Authorization": f"Bearer {denied}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PEMANGGIL_DITOLAK"
