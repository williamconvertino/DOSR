"""In-memory secret store keyed by name (demo only, not real security)."""

import hashlib
import hmac
import secrets


class Vault:
    def __init__(self, master_key: bytes | None = None):
        self._key = master_key or secrets.token_bytes(32)
        self._secrets: dict[str, bytes] = {}

    def put(self, name: str, value: bytes) -> None:
        self._secrets[name] = value

    def get(self, name: str) -> bytes:
        return self._secrets[name]

    def fingerprint(self, name: str) -> str:
        return hmac.new(self._key, self._secrets[name], hashlib.sha256).hexdigest()[:16]

    def rotate_key(self) -> None:
        """Replace the master key; existing fingerprints change."""
        self._key = secrets.token_bytes(32)
