import asyncio
from collections.abc import Iterable

import pytest

from kalpi_engine.brokers.base import InstrumentRef
from kalpi_engine.brokers.instruments import InstrumentResolver
from kalpi_engine.brokers.paper import PaperBroker
from kalpi_engine.brokers.registry import Registry
from kalpi_engine.domain.enums import Exchange
from kalpi_engine.domain.errors import UnknownSymbol


def parse(raw: bytes) -> Iterable[InstrumentRef]:
    for line in raw.decode().splitlines():
        sym, tok = line.split(",")
        yield InstrumentRef(exchange=Exchange.NSE, symbol=sym, token=tok)


class Source:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def __call__(self) -> bytes:
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.calls <= self.fail_first:
            raise ConnectionError("master download failed")
        return b"RELIANCE,2885\nTCS,11536\n"


async def test_master_fetched_once_even_with_concurrent_first_use() -> None:
    src = Source()
    r = InstrumentResolver(src, parse)
    refs = await asyncio.gather(*(r.resolve(Exchange.NSE, "reliance") for _ in range(10)))
    assert {ref.token for ref in refs} == {"2885"}
    assert await r.load() == 2
    assert src.calls == 1


async def test_unknown_symbol_and_wrong_exchange() -> None:
    r = InstrumentResolver(Source(), parse)
    with pytest.raises(UnknownSymbol):
        await r.resolve(Exchange.NSE, "NOPE")
    with pytest.raises(UnknownSymbol):
        await r.resolve(Exchange.BSE, "TCS")


async def test_failed_load_is_not_cached() -> None:
    src = Source(fail_first=1)
    r = InstrumentResolver(src, parse)
    with pytest.raises(ConnectionError):
        await r.load()
    assert not r.loaded
    assert (await r.resolve(Exchange.NSE, "TCS")).token == "11536"
    assert src.calls == 2


class BrokenStartup(PaperBroker):
    async def startup(self) -> None:
        raise ConnectionError("no network")


async def test_registry_startup_warms_all_and_survives_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reg = Registry({"broken": BrokenStartup, "paper": PaperBroker})
    await reg.startup()
    assert "startup of broker broken failed" in caplog.text
    assert isinstance(reg.get("paper"), PaperBroker)
