"""Fernet encryption for broker tokens at rest. The plaintext never leaves this boundary in logs."""

from cryptography.fernet import Fernet


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("FERNET_KEY is not set")
        self._f = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._f.decrypt(ciphertext.encode("ascii")).decode()

    def __repr__(self) -> str:
        return "TokenCipher(<redacted>)"
