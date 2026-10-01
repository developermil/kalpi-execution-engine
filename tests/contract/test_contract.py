"""Adapter contract suite (C0): every registered broker x every case in harness.CASES."""

import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from kalpi_engine import brokers
from kalpi_engine.brokers.registry import discover
from tests.contract.harness import CASES, FIXTURES_DIR, PlaceCheck, discover_fixtures, missing_hooks

FIXTURES = discover_fixtures()
DUMMY = Path(__file__).parent / "_dummy"


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("broker_id", sorted(FIXTURES))
async def test_contract(broker_id: str, case: str) -> None:
    await CASES[case](FIXTURES[broker_id])


def test_every_registered_broker_has_a_contract_fixture() -> None:
    assert set(discover()) <= set(FIXTURES), "add tests/contract/fixtures/<broker_id>.py"


# ---------- the suite itself must catch an incomplete or toothless fixture ----------


def _without(fx: ModuleType, *names: str) -> ModuleType:
    clone = ModuleType(f"{fx.__name__}_without_{'_'.join(names)}")
    clone.__dict__.update({k: v for k, v in vars(fx).items() if k not in names})
    return clone


@pytest.mark.parametrize(
    ("omitted", "case"),
    [("login", "login"), ("place_ok", "place_ok"), ("error_200", "error_200"),
     ("tag_hit", "tag_lookup"), ("tag_miss", "tag_lookup")],
)  # fmt: skip
async def test_suite_fails_fixture_missing_a_case(omitted: str, case: str) -> None:
    broken = _without(FIXTURES["paper"], omitted)
    assert omitted in missing_hooks(broken)
    with pytest.raises((pytest.fail.Exception, AssertionError)):
        await CASES["meta"](broken)
    with pytest.raises(pytest.fail.Exception, match=omitted):
        await CASES[case](broken)


async def test_suite_fails_place_ok_without_payload_assertion() -> None:
    real = FIXTURES["paper"].place_ok

    async def toothless(*args: object) -> PlaceCheck:
        check: PlaceCheck = await real(*args)
        return PlaceCheck(order_id=check.order_id, sent=check.sent, expect={})

    async def wrong(*args: object) -> PlaceCheck:
        check: PlaceCheck = await real(*args)
        return PlaceCheck(order_id=check.order_id, sent=check.sent, expect={"quantity": 99})

    for hook in (toothless, wrong):
        fx = _without(FIXTURES["paper"], "place_ok")
        fx.place_ok = hook
        with pytest.raises(AssertionError):
            await CASES["place_ok"](fx)


# ---------- onboarding proof: a 6th broker = brokers/<id>.py + fixtures/<id>.py ----------


@pytest.fixture
def dummy_broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setattr(brokers, "__path__", [*brokers.__path__, str(DUMMY / "brokers")])
    yield discover_fixtures(FIXTURES_DIR, DUMMY / "fixtures")["dummy"]
    sys.modules.pop("kalpi_engine.brokers.dummy", None)


def test_dummy_broker_is_discovered_without_shared_edits(dummy_broker: ModuleType) -> None:
    ids = set(discover())
    assert "dummy" in ids
    assert ids <= set(discover_fixtures(FIXTURES_DIR, DUMMY / "fixtures"))


@pytest.mark.parametrize("case", list(CASES))
async def test_dummy_http_broker_passes_contract(dummy_broker: ModuleType, case: str) -> None:
    await CASES[case](dummy_broker)


async def test_signing_broker_needs_checksum_vectors(dummy_broker: ModuleType) -> None:
    broken = _without(dummy_broker, "checksum_vectors")
    assert missing_hooks(broken) == ["checksum_vectors"]
    with pytest.raises(pytest.fail.Exception, match="checksum_vectors"):
        await CASES["checksum"](broken)
