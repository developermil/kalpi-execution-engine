from fastapi.testclient import TestClient

from kalpi_engine.config import Settings
from kalpi_engine.main import create_app


def test_healthz_and_readyz_return_200() -> None:
    client = TestClient(create_app(Settings()))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_settings_hide_secrets() -> None:
    s = Settings(fernet_key="topsecret", api_keys="demo:k")  # type: ignore[arg-type]
    assert "topsecret" not in repr(s)
    assert s.lease_ttl_s == 30
