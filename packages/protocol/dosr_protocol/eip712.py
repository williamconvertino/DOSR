"""EIP-712 typed data for attestor attestations (docs/ARCHITECTURE.md, "Recommended signed domain").

Every DOSR content hash (policy, bundle, request, review input, response) is
SHA-256 (docs/HASHING.md). Keccak-256 only appears inside EIP-712's own struct
hashing, because that is the digest the EVM's `ecrecover` checks; the SHA-256
values are just `bytes32` fields to it.

Building the typed data is dependency-free; signing/recovery needs `eth-account`.
"""

from . import PROTOCOL_VERSION
from .canonical import unqualify_oid

DOMAIN_NAME = "DOSR"

DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

# (EIP-712 field, Solidity type, attestation JSON key)
ATTESTATION_FIELDS = [
    ("repoId", "bytes32", "repo_id"),
    ("policyHash", "bytes32", "policy_hash"),
    ("parentGitOid", "bytes32", "parent_git_oid"),
    ("candidateGitOid", "bytes32", "candidate_git_oid"),
    ("bundleSha256", "bytes32", "bundle_sha256"),
    ("bundleCid", "string", "bundle_cid"),
    ("provider", "string", "provider"),
    ("model", "string", "model"),
    ("modelVersion", "string", "model_version"),
    ("approved", "bool", "approved"),
    ("requestHash", "bytes32", "request_hash"),
    ("reviewInputHash", "bytes32", "review_input_hash"),
    ("responseHash", "bytes32", "response_hash"),
    ("issuedAt", "uint64", "issued_at"),
    ("expiresAt", "uint64", "expires_at"),
    ("attestorKeyId", "string", "attestor_key_id"),
]


def oid_to_bytes32(oid: str) -> bytes:
    """`sha1:<40 hex>` is right-padded with zeros, matching Solidity's
    `bytes32(bytes20(x))`; `sha256:<64 hex>` fills the field exactly."""
    fmt, hex_oid = unqualify_oid(oid)
    raw = bytes.fromhex(hex_oid)
    if (fmt, len(raw)) not in (("sha1", 20), ("sha256", 32)):
        raise ValueError(f"unsupported Git OID {oid!r}")
    return raw.ljust(32, b"\0")


def _bytes32(value: str) -> bytes:
    """`0x<64 hex>` or `sha256:<64 hex>` -> 32 bytes."""
    hex_part = value[2:] if value.startswith("0x") else value.partition("sha256:")[2]
    raw = bytes.fromhex(hex_part)
    if len(raw) != 32:
        raise ValueError(f"not a 32-byte value: {value!r}")
    return raw


def typed_data(att: dict) -> dict:
    """Full EIP-712 message for an attestation dict shaped like examples/wire/3.*."""
    message = {}
    for name, sol_type, key in ATTESTATION_FIELDS:
        value = att[key]
        if key in ("parent_git_oid", "candidate_git_oid"):
            value = oid_to_bytes32(value)
        elif sol_type == "bytes32":
            value = _bytes32(value)
        elif key == "bundle_cid":
            value = value or ""
        message[name] = value
    return {
        "types": {
            "EIP712Domain": DOMAIN_TYPE,
            "Attestation": [{"name": n, "type": t} for n, t, _ in ATTESTATION_FIELDS],
        },
        "primaryType": "Attestation",
        "domain": {
            "name": DOMAIN_NAME,
            "version": PROTOCOL_VERSION,
            "chainId": att["chain_id"],
            "verifyingContract": att["verifying_contract"],
        },
        "message": message,
    }


def recover_signer(att: dict, signature: str) -> str:
    """Address that produced `signature` over `att` (checksummed)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    return Account.recover_message(encode_typed_data(full_message=typed_data(att)), signature=signature)
