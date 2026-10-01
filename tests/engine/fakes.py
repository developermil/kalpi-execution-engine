import asyncio


class FakeClock:
    """Monotonic fake: sleep(d) wakes at its own target time, so concurrent sleepers compose."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        target = self.t + d
        await asyncio.sleep(0)
        self.t = max(self.t, target)
