from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_astria_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uji tidak boleh membaca ASTRIA_* milik kontainer dev (mis. ASTRIA_ENV=dev)."""
    import os

    for key in list(os.environ):
        if key.startswith("ASTRIA_") and not key.startswith("ASTRIA_TEST_"):
            monkeypatch.delenv(key, raising=False)
