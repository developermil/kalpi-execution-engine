"""Auto-discovery: every module in brokers/ defining a concrete BrokerAdapter is registered."""

import importlib
import inspect
import pkgutil
from functools import lru_cache

from kalpi_engine import brokers
from kalpi_engine.brokers.base import BrokerAdapter, BrokerMeta

_NOT_ADAPTERS = {"base", "registry", "instruments"}


def discover() -> dict[str, type[BrokerAdapter]]:
    """Import brokers/*.py and return {meta.id: adapter class}. Duplicate ids are an error."""
    found: dict[str, type[BrokerAdapter]] = {}
    for mod_info in pkgutil.iter_modules(brokers.__path__):
        if mod_info.name in _NOT_ADAPTERS or mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{brokers.__name__}.{mod_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, BrokerAdapter)
                and cls.__module__ == module.__name__
                and not inspect.isabstract(cls)
                and isinstance(getattr(cls, "meta", None), BrokerMeta)
            ):
                if cls.meta.id in found:
                    raise RuntimeError(f"duplicate broker id {cls.meta.id!r} in {module.__name__}")
                found[cls.meta.id] = cls
    return dict(sorted(found.items()))


class Registry:
    """One adapter instance per broker id (adapters share their http client across sessions)."""

    def __init__(self, classes: dict[str, type[BrokerAdapter]]) -> None:
        self._classes = classes
        self._instances: dict[str, BrokerAdapter] = {}

    def ids(self) -> list[str]:
        return list(self._classes)

    def metas(self) -> list[BrokerMeta]:
        return [cls.meta for cls in self._classes.values()]

    def get(self, broker_id: str) -> BrokerAdapter:
        if broker_id not in self._classes:
            raise KeyError(broker_id)
        if broker_id not in self._instances:
            self._instances[broker_id] = self._classes[broker_id]()
        return self._instances[broker_id]

    async def aclose(self) -> None:
        for adapter in self._instances.values():
            await adapter.aclose()


@lru_cache
def get_registry() -> Registry:
    return Registry(discover())
