"""Klien HTTP keluar: batas waktu pasti dan tanpa redirect (pola anti-SSRF)."""

from __future__ import annotations

import httpx


def make_client(base_url: str = "", *, timeout: float = 5.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout, connect=3.0),
        follow_redirects=False,
        headers={"User-Agent": "astria-service"},
    )
