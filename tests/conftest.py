from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_SPAN_EXPORTER: list[object] = []


@pytest.fixture
def otel():
    """OpenTelemetry aktif dengan exporter di memori; menghasilkan exporter.

    TracerProvider global hanya bisa dipasang sekali per proses, jadi dipasang pada pemakaian pertama dan dipakai
    bersama. Setelah uji, penanda OTel Astria dimatikan lagi sehingga uji lain berjalan tanpa span.
    """
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from astria_common import _otel, tracing

    if not _SPAN_EXPORTER:
        exporter = InMemorySpanExporter()
        _otel.install_provider("uji", processors=[SimpleSpanProcessor(exporter)])
        _SPAN_EXPORTER.append(exporter)
    exporter = _SPAN_EXPORTER[0]
    exporter.clear()
    tracing.enable_otel(True)
    yield exporter
    tracing.enable_otel(False)
    exporter.clear()


@pytest.fixture(autouse=True)
def _clean_astria_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uji tidak boleh membaca ASTRIA_* milik kontainer dev (mis. ASTRIA_ENV=dev)."""
    import os

    for key in list(os.environ):
        if key.startswith("ASTRIA_") and not key.startswith("ASTRIA_TEST_"):
            monkeypatch.delenv(key, raising=False)
