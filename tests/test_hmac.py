from __future__ import annotations

import pytest

from astria_common.hmac_auth import H_SIG, H_TS, HmacError, NonceCache, signed_headers, verify

KEY = bytes(range(32))
BODY = b'{"audience":"svc:audit"}'


def test_roundtrip() -> None:
    headers = signed_headers("ingest", KEY, "POST", "/internal/v1/service-tokens", BODY, now=1000)
    verify(KEY, "POST", "/internal/v1/service-tokens", headers, BODY, NonceCache(), now=1010)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda h, b: (h, b + b" "), "tanda_tangan_salah"),
        (lambda h, b: (h | {H_SIG: "0" * 64}, b), "tanda_tangan_salah"),
        (lambda h, b: (h | {H_TS: "1"}, b), "cap_waktu_di_luar_jendela"),
        (lambda h, b: ({k: v for k, v in h.items() if k != H_SIG}, b), "header_tidak_lengkap"),
    ],
)
def test_rejects_tampering(mutate, reason: str) -> None:
    headers = signed_headers("ingest", KEY, "POST", "/p", BODY, now=1000)
    headers, body = mutate(headers, BODY)
    with pytest.raises(HmacError) as err:
        verify(KEY, "POST", "/p", headers, body, NonceCache(), now=1000)
    assert err.value.reason == reason


def test_rejects_wrong_key_and_path() -> None:
    headers = signed_headers("ingest", KEY, "POST", "/p", BODY, now=1000)
    with pytest.raises(HmacError):
        verify(b"x" * 32, "POST", "/p", headers, BODY, NonceCache(), now=1000)
    with pytest.raises(HmacError):
        verify(KEY, "POST", "/other", headers, BODY, NonceCache(), now=1000)


def test_rejects_replayed_nonce() -> None:
    nonces = NonceCache()
    headers = signed_headers("ingest", KEY, "POST", "/p", BODY, now=1000)
    verify(KEY, "POST", "/p", headers, BODY, nonces, now=1000)
    with pytest.raises(HmacError) as err:
        verify(KEY, "POST", "/p", headers, BODY, nonces, now=1001)
    assert err.value.reason == "nonce_terpakai"
