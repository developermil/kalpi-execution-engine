"""Broker/engine error taxonomy (SPEC §4). Adapters raise these; the executor branches on type."""


class KalpiError(Exception):
    code = "INTERNAL"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class AuthExpired(KalpiError):
    code = "AUTH_EXPIRED"


class RateLimited(KalpiError):
    """Retry-safe: the broker refused before accepting the order."""

    code = "RATE_LIMITED"

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InvalidOrder(KalpiError):
    code = "INVALID_ORDER"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class InsufficientFunds(KalpiError):
    code = "INSUFFICIENT_FUNDS"


class BrokerRejected(KalpiError):
    code = "BROKER_REJECTED"


class TransientError(KalpiError):
    """Request provably never reached the broker; safe to retry."""

    code = "TRANSIENT"


class AmbiguousSubmit(KalpiError):
    """Order may or may not exist at the broker: reconcile by tag, never resend (D9)."""

    code = "AMBIGUOUS_SUBMIT"


class UnknownSymbol(KalpiError):
    code = "UNKNOWN_SYMBOL"
