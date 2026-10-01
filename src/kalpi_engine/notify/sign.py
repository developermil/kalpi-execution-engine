"""HMAC-SHA256 webhook signatures: header `X-Kalpi-Signature: sha256=<hex>` over the raw body."""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Kalpi-Signature"
_PREFIX = "sha256="


def sign(secret: str, body: bytes) -> str:
    return _PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith(_PREFIX):
        return False
    return hmac.compare_digest(sign(secret, body), header)
