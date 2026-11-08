from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astria_common.app import create_app
from astria_common.component import Check
from astria_common.envguard import EnvironmentGuardError
from astria_common.errors import AstriaError
from astria_common.settings import AstriaSettings


class Fake:
    def __init__(self, name: str, status: str, required: bool = True) -> None:
        self.name = name
        self.required = required
        self.status = status
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def check(self) -> Check:
        return Check(self.status)  # type: ignore[arg-type]


def settings(**kw) -> AstriaSettings:
    return AstriaSettings(service_name="demo-svc", env="test", **kw)


def test_health_and_ready_ok() -> None:
    comp = Fake("postgres", "ok")
    with TestClient(create_app(settings(), components=[comp])) as client:
        assert comp.started
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["service"] == "demo-svc"
        assert health["checks"]["postgres"] == {"status": "ok"}
        assert client.get("/ready").status_code == 200
    assert not comp.started


def test_ready_is_503_when_required_component_down() -> None:
    comps = [Fake("postgres", "down"), Fake("outbox", "degraded", required=False)]
    with TestClient(create_app(settings(), components=comps)) as client:
        assert client.get("/health").status_code == 200
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "down"


def test_optional_component_does_not_block_ready() -> None:
    comps = [Fake("postgres", "ok"), Fake("outbox", "down", required=False)]
    with TestClient(create_app(settings(), components=comps)) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/health").json()["status"] == "down"


def test_metrics_endpoint() -> None:
    with TestClient(create_app(settings())) as client:
        body = client.get("/metrics").text
        assert "astria_service_info" in body


def test_error_shape_and_trace_header() -> None:
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise AstriaError("CONTOH_GALAT", "contoh", 409)

    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    with TestClient(create_app(settings(), routers=[router])) as client:
        resp = client.get("/boom", headers={"traceparent": f"00-{trace}-00f067aa0ba902b7-01"})
        assert resp.status_code == 409
        assert resp.headers["X-Trace-Id"] == trace
        assert resp.json() == {"error": {"code": "CONTOH_GALAT", "message": "contoh", "trace_id": trace}}
        missing = client.get("/nope")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "TIDAK_DITEMUKAN"


def test_guard_runs_before_app_exists() -> None:
    bad = AstriaSettings(service_name="x", env="dev", database_url=SecretStr("postgresql+psycopg://u:p@h/astria"))
    with pytest.raises(EnvironmentGuardError):
        create_app(bad)
