"""Pure status rules: leg -> run (D28) and sell barrier (D21)."""

from collections.abc import Iterable

from kalpi_engine.domain.enums import LegStatus, RunStatus

_EXECUTED = frozenset({LegStatus.FILLED, LegStatus.PARTIAL})
# Non-terminal statuses at finalisation mean the order may exist: in doubt, like UNKNOWN.
_IN_DOUBT = frozenset(
    {
        LegStatus.OPEN,
        LegStatus.UNKNOWN,
        LegStatus.PLANNED,
        LegStatus.SUBMITTING,
        LegStatus.SUBMITTED,
    }
)


def run_status(legs: Iterable[LegStatus]) -> RunStatus:
    statuses = list(legs)
    if not statuses:
        raise ValueError("a run has at least one leg")
    if all(s is LegStatus.FILLED for s in statuses):
        return RunStatus.COMPLETED
    if not any(s in _EXECUTED or s in _IN_DOUBT for s in statuses):
        return RunStatus.FAILED
    return RunStatus.COMPLETED_WITH_FAILURES


def sell_failed(leg: LegStatus) -> bool:
    """A sell is resolved-OK only when FILLED; anything else counts for halt_on_sell_failure."""
    return leg is not LegStatus.FILLED
