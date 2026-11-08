from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from astria_common.envguard import EnvironmentGuardError, enforce, violations
from astria_common.settings import AstriaSettings


class NarrativeLike(AstriaSettings):
    model_backend: str | None = None
    model_api_key: SecretStr | None = None
    dev_endpoints: bool = False


def make(env: str, db: str | None = None, **extra: object) -> NarrativeLike:
    url = SecretStr(f"postgresql+psycopg://u:p@postgres:5432/{db}") if db else None
    return NarrativeLike(service_name="narrative", env=env, database_url=url, **extra)


def test_env_is_required() -> None:
    with pytest.raises(ValidationError):
        AstriaSettings(service_name="x")  # type: ignore[call-arg]


def test_unknown_env_rejected() -> None:
    with pytest.raises(ValidationError):
        AstriaSettings(service_name="x", env="staging")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("env", "db", "extra"),
    [
        ("dev", "ingest_dev", {}),
        ("dev", None, {"dev_endpoints": True}),
        ("demo", "ingest_demo", {}),
        ("prod", "astria", {"model_backend": "lokal"}),
        ("test", "anything", {}),
    ],
)
def test_allowed(env: str, db: str | None, extra: dict) -> None:
    assert violations(make(env, db, **extra)) == []


@pytest.mark.parametrize(
    ("env", "db", "extra", "fragment"),
    [
        ("dev", "astria", {}, "_dev"),
        ("demo", "ingest_dev", {}, "_demo"),
        ("prod", "ingest_dev", {}, "dev/demo"),
        ("prod", "astria", {"model_backend": "replay"}, "replay"),
        ("prod", "astria", {"model_api_key": SecretStr("k")}, "kunci API"),
        ("demo", "x_demo", {"dev_endpoints": True}, "endpoint dev"),
    ],
)
def test_forbidden_config_refuses_to_start(env: str, db: str, extra: dict, fragment: str) -> None:
    settings = make(env, db, **extra)
    with pytest.raises(EnvironmentGuardError) as err:
        enforce(settings)
    assert any(fragment in p for p in err.value.problems)


def test_service_key_from_file(tmp_path) -> None:
    path = tmp_path / "key"
    path.write_text("00ff\n", encoding="utf-8")
    settings = AstriaSettings(service_name="x", env="test", service_key_file=path)
    assert settings.service_key_bytes() == b"\x00\xff"
    assert settings.principal == "svc:x"
