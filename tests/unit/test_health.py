from pathlib import Path

from fastapi.testclient import TestClient

from kalpi_engine.config import Settings
from kalpi_engine.main import create_app


def test_healthz_and_readyz_return_200(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "k.db"
    app = create_app(Settings(database_url=f"sqlite+aiosqlite:///{db}"))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
    assert db.exists()


def test_tables_created_at_startup(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "k.db"
    with TestClient(create_app(Settings(database_url=f"sqlite+aiosqlite:///{db}"))):
        pass
    names = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master")}
    assert {"runs", "legs", "events", "outbox", "broker_sessions"} <= names


def test_settings_hide_secrets() -> None:
    s = Settings(fernet_key="topsecret", api_keys="demo:k")  # type: ignore[arg-type]
    assert "topsecret" not in repr(s)
    assert s.lease_ttl_s == 30
