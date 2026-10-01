import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalpi_engine import brokers
from kalpi_engine.brokers.registry import Registry, discover
from kalpi_engine.config import Settings
from kalpi_engine.main import create_app

DROP_IN = '''
from kalpi_engine.brokers.paper import PaperBroker
from kalpi_engine.brokers.base import AuthMode, BrokerMeta, RateLimits


class DropInBroker(PaperBroker):
    meta = BrokerMeta(id="dropin", name="Drop-in", auth_mode=AuthMode.NONE,
                      rate_limits=RateLimits(orders_per_sec=1, reads_per_sec=1))
'''


@pytest.fixture
def dropped_file() -> Iterator[Path]:
    """Write a brand-new module into brokers/ (no other file edited), remove it afterwards."""
    path = Path(brokers.__path__[0]) / "zz_dropin_test.py"
    path.write_text(DROP_IN, encoding="utf-8")
    importlib.invalidate_caches()
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        sys.modules.pop(f"{brokers.__name__}.zz_dropin_test", None)


def test_paper_is_discovered() -> None:
    assert "paper" in discover()


def test_new_file_in_brokers_dir_registers_itself(dropped_file: Path) -> None:
    found = discover()
    assert "dropin" in found and "paper" in found
    # PaperBroker is re-exported in the new module but registered only once, under its own module.
    assert found["paper"].__module__ == "kalpi_engine.brokers.paper"


def test_dropped_file_gone_after_cleanup() -> None:
    assert "dropin" not in discover()


def test_registry_reuses_one_instance_and_rejects_unknown() -> None:
    reg = Registry(discover())
    assert reg.get("paper") is reg.get("paper")
    with pytest.raises(KeyError):
        reg.get("nope")


def test_get_brokers_lists_paper_with_flags() -> None:
    client = TestClient(create_app(Settings(), Registry(discover())))
    rows = client.get("/v1/brokers").json()
    paper = next(r for r in rows if r["id"] == "paper")
    for key in ("experimental", "live_tested", "market_order_verified", "auth_mode",
                "credential_fields", "rate_limits", "requires_static_ip", "daily_2fa"):
        assert key in paper, key  # fmt: skip
    assert paper["live_tested"] is False and paper["auth_mode"] == "NONE"
