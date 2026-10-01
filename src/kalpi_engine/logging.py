"""JSON logs with run/leg/request ids and secret redaction (D11, R9). Stdlib only.

Every record is one JSON line. `run_id`, `leg_id` and `request_id` come from context variables,
so any log call made while a run/leg/request is active carries them without plumbing.
Secrets are scrubbed from the message, extra fields and tracebacks before anything is written.
"""

import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_CTX: dict[str, ContextVar[str | None]] = {
    name: ContextVar(name, default=None) for name in ("run_id", "leg_id", "request_id")
}
REDACTED = "[REDACTED]"
_KEYS = (
    "access_token|refresh_token|feed_token|request_token|accesstoken|refreshtoken|feedtoken|"
    "requesttoken|jwttoken|jwt|token|password|passwd|pin|clientsecret|"
    "totp_secret|totp|secret|api_key|api_secret|apikey|x-api-key|checksum|auth_code|private_key"
)
# key=value, key: value, "key": "value" (JSON/dict repr/form), value runs to a delimiter.
_KV = re.compile(
    rf"""(?ix)(["']?\b(?:\w*_)?(?:{_KEYS})["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}}&"')]+)"""
)
_AUTH = re.compile(r"""(?i)(authorization["']?\s*[:=]\s*["']?)[^"'\n,}]+""")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=:-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_SENSITIVE = re.compile(rf"(?i)^(?:\w*_)?(?:{_KEYS}|authorization)$")


def redact_text(text: str) -> str:
    text = _AUTH.sub(rf"\1{REDACTED}", text)
    text = _KV.sub(
        lambda m: (
            f'{m.group(1)}"{REDACTED}"' if m.group(2)[:1] in "\"'" else f"{m.group(1)}{REDACTED}"
        ),
        text,
    )
    return _JWT.sub(REDACTED, _BEARER.sub(f"Bearer {REDACTED}", text))


def redact(value: Any) -> Any:
    """Recursively scrub strings; values under sensitive keys are replaced outright."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: REDACTED if _SENSITIVE.match(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact(v) for v in value]
    return value


@contextmanager
def log_context(**ids: str | None) -> Iterator[None]:
    """Bind run_id / leg_id / request_id for the enclosed code (and tasks it spawns)."""
    tokens = [(_CTX[k], _CTX[k].set(v)) for k, v in ids.items()]
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def current_context() -> dict[str, str]:
    return {k: v for k, var in _CTX.items() if (v := var.get())}


_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            **current_context(),
        }
        extras = {k: v for k, v in vars(record).items() if k not in _STD}
        doc |= redact(extras)  # sensitive keys are replaced outright, other values scrubbed
        if record.exc_info:
            doc["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(doc, default=str)


_MARK = "_kalpi_json"


def setup_logging(level: str = "INFO") -> None:
    """Install one JSON handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if any(getattr(h, _MARK, False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    setattr(handler, _MARK, True)
    root.addHandler(handler)
