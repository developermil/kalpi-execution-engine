"""Instrument master cache shared by HTTP adapters (D13, D35).

The master file is fetched and parsed once per process (startup warm-up or first use) and never
refreshed on a schedule: refresh = restart. A failed load is not cached, so the next call retries.
Adapters supply only `fetch` (download bytes) and `parse` (rows -> InstrumentRef).
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from kalpi_engine.brokers.base import InstrumentRef
from kalpi_engine.domain.enums import Exchange
from kalpi_engine.domain.errors import UnknownSymbol

Fetch = Callable[[], Awaitable[bytes]]
Parse = Callable[[bytes], Iterable[InstrumentRef]]


class InstrumentResolver:
    def __init__(self, fetch: Fetch, parse: Parse) -> None:
        self._fetch = fetch
        self._parse = parse
        self._by_key: dict[tuple[Exchange, str], InstrumentRef] | None = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._by_key is not None

    async def load(self) -> int:
        """Fetch + parse the master once; concurrent callers share one download."""
        async with self._lock:
            if self._by_key is None:
                raw = await self._fetch()
                self._by_key = {(ref.exchange, ref.symbol.upper()): ref for ref in self._parse(raw)}
            return len(self._by_key)

    async def resolve(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        if self._by_key is None:
            await self.load()
        assert self._by_key is not None
        ref = self._by_key.get((exchange, symbol.upper()))
        if ref is None:
            raise UnknownSymbol(f"{exchange}:{symbol}")
        return ref
