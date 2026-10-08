"""Canonical serialization, hashing, and identifier helpers.

`canonical_json` produces RFC 8785 (JCS) output for the JSON values DOSR uses
(objects, arrays, strings, integers, booleans, null). Floats are rejected rather
than risk a JCS number-formatting mismatch; no DOSR payload needs them.
"""

import hashlib
import json
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _reject_floats(value):
    if isinstance(value, float):
        raise TypeError("canonical_json does not support floats")
    if isinstance(value, dict):
        for v in value.values():
            _reject_floats(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_floats(v)


def canonical_json(value) -> bytes:
    _reject_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_0x(data: bytes) -> str:
    """`0x` + 64 hex: the format used for repo/policy/request hashes."""
    return "0x" + hashlib.sha256(data).hexdigest()


def sha256_qualified(data: bytes) -> str:
    """`sha256:` + 64 hex: the format used for bundle digests."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file_qualified(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def policy_hash(policy: dict) -> str:
    """Hash of the immutable policy, as registered on-chain at genesis."""
    return sha256_0x(canonical_json(policy))


def derive_repo_id(name: str, policy_hash_hex: str, creator: str, nonce: str | None = None) -> str:
    """Prototype repo ID. A real deployment will likely have the contract assign it
    (e.g. keccak256 of the genesis registration)."""
    nonce = nonce or os.urandom(16).hex()
    return sha256_0x(
        canonical_json(
            {"name": name, "policy_hash": policy_hash_hex, "creator": creator, "nonce": nonce}
        )
    )


def qualify_oid(object_format: str, hex_oid: str) -> str:
    return f"{object_format}:{hex_oid}"


def unqualify_oid(oid: str) -> tuple[str, str]:
    fmt, _, hex_oid = oid.partition(":")
    if not hex_oid:
        raise ValueError(f"not an algorithm-qualified Git OID: {oid!r}")
    return fmt, hex_oid


def new_request_id(now_ms: int | None = None) -> str:
    """26-char ULID (48-bit ms timestamp + 80 random bits, Crockford base32)."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))
