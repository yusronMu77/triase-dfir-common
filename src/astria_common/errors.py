"""Bentuk galat seragam: `{"error": {"code", "message", "trace_id"}}` (kontrak openapi/common.yaml)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from astria_common.log import get_logger
from astria_common.tracing import current_trace_id

log = get_logger(__name__)

_HTTP_CODES = {
    400: "PERMINTAAN_TIDAK_VALID",
    401: "TIDAK_TERAUTENTIKASI",
    403: "DILARANG",
    404: "TIDAK_DITEMUKAN",
    405: "METODE_TIDAK_DIDUKUNG",
    409: "KONFLIK",
    413: "TERLALU_BESAR",
    422: "TIDAK_DAPAT_DIPROSES",
    429: "TERLALU_BANYAK",
    503: "TIDAK_TERSEDIA",
}


class AstriaError(Exception):
    """Galat yang aman ditampilkan ke pemanggil. `message` tidak boleh memuat rahasia atau data mentah."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "trace_id": current_trace_id()}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AstriaError)
    async def _astria(_req: Request, exc: AstriaError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return JSONResponse(
            error_body("VALIDASI_GAGAL", "field tidak valid: " + ", ".join(f for f in fields if f)), status_code=400
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "GALAT_HTTP")
        message = exc.detail if isinstance(exc.detail, str) else code
        return JSONResponse(error_body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception) -> JSONResponse:
        log.exception("galat_tak_tertangani", error_type=type(exc).__name__)
        return JSONResponse(error_body("GALAT_INTERNAL", "galat internal"), status_code=500)
