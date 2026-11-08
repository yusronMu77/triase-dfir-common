"""Pabrik aplikasi FastAPI untuk semua layanan Astria."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from astria_common import envguard
from astria_common.auth import JwksSource, ServiceTokenVerifier
from astria_common.component import Check, Component
from astria_common.errors import install_error_handlers
from astria_common.log import configure_logging, get_logger
from astria_common.metrics import HTTP_REQUESTS, SERVICE_INFO
from astria_common.settings import AstriaSettings
from astria_common.tracing import install_trace_middleware, setup_otel

log = get_logger(__name__)

_QUIET_PATHS = {"/health", "/ready", "/metrics"}


def _aggregate(checks: dict[str, Check], required: dict[str, bool], *, ready: bool) -> str:
    states = [c.status for name, c in checks.items() if required.get(name, True) or not ready]
    if any(s == "down" for s in states):
        return "down"
    if any(s == "degraded" for s in states):
        return "degraded"
    return "ok"


async def _safe_check(component: Component) -> Check:
    try:
        return await asyncio.wait_for(component.check(), timeout=3)
    except Exception as exc:  # noqa: BLE001 - status komponen tidak boleh menjatuhkan endpoint health
        return Check("down", f"{type(exc).__name__}")


def create_app(
    settings: AstriaSettings,
    *,
    components: Sequence[Component] = (),
    routers: Sequence[APIRouter] = (),
    title: str | None = None,
    on_startup: Callable[[FastAPI], Awaitable[None]] | None = None,
    token_verifier: ServiceTokenVerifier | None = None,
) -> FastAPI:
    """Buat aplikasi layanan. Pagar ASTRIA_ENV diperiksa di sini, sebelum port dibuka."""
    configure_logging(settings.service_name, settings.env, settings.log_level)
    envguard.enforce(settings)
    SERVICE_INFO.info({"service": settings.service_name, "version": settings.version, "env": settings.env})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started: list[Component] = []
        for component in components:
            await component.start()
            started.append(component)
        if on_startup is not None:
            await on_startup(app)
        log.info("layanan_mulai", version=settings.version, components=[c.name for c in components])
        try:
            yield
        finally:
            for component in reversed(started):
                try:
                    await component.stop()
                except Exception:  # noqa: BLE001
                    log.exception("komponen_gagal_berhenti", component=component.name)
            log.info("layanan_berhenti")

    app = FastAPI(
        title=title or f"astria-{settings.service_name}",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs" if settings.env in {"dev", "test"} else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env in {"dev", "test"} else None,
    )
    app.state.settings = settings
    app.state.components = {c.name: c for c in components}
    app.state.token_verifier = token_verifier or ServiceTokenVerifier(JwksSource(settings.identity_url))
    install_error_handlers(app)

    @app.middleware("http")
    async def _access(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        if request.url.path not in _QUIET_PATHS:
            log.info("http", method=request.method, route=path, status=response.status_code)
        return response

    # Ditambahkan terakhir = lapisan terluar: log akses di atas sudah membawa trace_id.
    install_trace_middleware(app)
    setup_otel(app, settings.service_name, settings.otlp_endpoint)

    async def _report(*, ready: bool) -> tuple[int, dict]:
        results = await asyncio.gather(*(_safe_check(c) for c in components))
        checks = {c.name: r for c, r in zip(components, results, strict=True)}
        required = {c.name: c.required for c in components}
        status = _aggregate(checks, required, ready=ready)
        body = {
            "status": status,
            "service": settings.service_name,
            "version": settings.version,
            "env": settings.env,
            "checks": {name: check.as_dict() for name, check in checks.items()},
        }
        code = 503 if ready and status == "down" else 200
        return code, body

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        _, body = await _report(ready=False)
        return JSONResponse(body)

    @app.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        code, body = await _report(ready=True)
        return JSONResponse(body, status_code=code)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for router in routers:
        app.include_router(router)
    return app


def run(app: FastAPI | str, settings: AstriaSettings) -> None:
    """Jalankan uvicorn dengan log JSON dari astria_common (bukan konfigurasi log bawaan uvicorn)."""
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=False)
