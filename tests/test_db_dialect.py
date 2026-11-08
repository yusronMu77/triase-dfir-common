"""PostgreSQL di server, SQLite untuk proses lokal (keputusan pemilik 2026-11-08)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select, text

from astria_common.db import Database, dialect_of, normalize_url
from astria_common.envguard import EnvironmentGuardError, database_name, enforce, violations
from astria_common.settings import AstriaSettings


def make(env: str, url: str) -> AstriaSettings:
    return AstriaSettings(service_name="gateway", env=env, database_url=SecretStr(url))


def test_dialect_from_url_scheme() -> None:
    assert dialect_of("postgresql+psycopg://u:p@h/db_dev") == "postgresql"
    assert dialect_of("postgresql://u:p@h/db_dev") == "postgresql"
    assert dialect_of("sqlite+aiosqlite:///./x_dev.db") == "sqlite"
    assert dialect_of("sqlite:///./x_dev.db") == "sqlite"
    with pytest.raises(ValueError, match="tidak didukung"):
        dialect_of("mysql://u:p@h/db")


def test_normalize_adds_async_driver() -> None:
    assert normalize_url("postgresql://u:p@h:5432/db_dev").startswith("postgresql+psycopg://u:p@h:5432/")
    assert normalize_url("sqlite:///./x.db").startswith("sqlite+aiosqlite://")
    already = "postgresql+psycopg://u:p@h/db"
    assert normalize_url(already) == already


def test_settings_reject_unsupported_dialect() -> None:
    with pytest.raises(ValidationError):
        make("dev", "mysql://u:p@h/db_dev")


def test_sqlite_database_name_is_file_stem() -> None:
    assert database_name(make("dev", "sqlite+aiosqlite:///./data/gateway_dev.db")) == "gateway_dev"
    assert database_name(make("dev", "sqlite+aiosqlite:///C:/data/gateway_dev.db")) == "gateway_dev"


def test_sqlite_allowed_in_dev_demo_test_for_local_process() -> None:
    assert violations(make("dev", "sqlite+aiosqlite:///./gateway_dev.db")) == []
    assert violations(make("demo", "sqlite+aiosqlite:///./gateway_demo.db")) == []
    assert violations(make("test", "sqlite+aiosqlite:///:memory:")) == []


def test_sqlite_name_suffix_still_guarded() -> None:
    assert violations(make("dev", "sqlite+aiosqlite:///./gateway.db"))
    assert violations(make("demo", "sqlite+aiosqlite:///./gateway_dev.db"))


def test_prod_rejects_sqlite_but_accepts_postgresql() -> None:
    with pytest.raises(EnvironmentGuardError, match="PostgreSQL"):
        enforce(make("prod", "sqlite+aiosqlite:///./gateway.db"))
    assert violations(make("prod", "postgresql+psycopg://u:p@postgres:5432/gateway")) == []


@pytest.mark.anyio
async def test_sqlite_engine_boots_creates_tables_and_sets_pragmas(tmp_path: Path) -> None:
    md = MetaData()
    items = Table("items", md, Column("id", Integer, primary_key=True), Column("name", String, nullable=False))
    url = SecretStr(f"sqlite+aiosqlite:///{(tmp_path / 'gateway_dev.db').as_posix()}")
    # `schema` diabaikan di SQLite (tidak ada skema), tidak boleh error.
    db = Database(url, schema="gateway", metadata=[md])
    assert db.dialect == "sqlite"
    assert db.name == "sqlite"
    await db.start()
    try:
        await db.wait_ready(timeout=10)
        assert (await db.check()).status == "ok"
        async with db.engine.begin() as conn:
            await conn.execute(insert(items).values(name="a"))
            assert (await conn.execute(select(items.c.name))).scalar_one() == "a"
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
    finally:
        await db.stop()
