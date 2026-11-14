"""Attestor settings, read from the environment.

    ATTESTOR_PRIVATE_KEY   secp256k1 signing key (default: public dev key below)
    ATTESTOR_KEY_ID        key id repositories list in policy.attestors
    DOSR_CHAIN             evm | mock (default: evm if DOSR_RPC_URL is set, else mock)
    DOSR_RPC_URL           EVM JSON-RPC endpoint, e.g. http://127.0.0.1:8545 (anvil in the demo)
    DOSR_CONTRACT          DOSRRegistry address (default: the dev deployment address)
    DOSR_CHAIN_STATE       mock chain state file (default: MockChain's default); forces the mock
    ATTESTOR_CACHE_DIR     per-repository Git object cache (lets clients send thin bundles)
    ATTESTOR_MAX_BUNDLE_BYTES
    ATTESTOR_PROVIDERS_FILE  JSON of extra OpenAI-compatible model providers (see reviewers.py)
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dosr_chain import MockChain, connect

# Hardhat/Anvil's well-known test account #1 (address 0x70997970C51812dc3A010C7d01b50e0d17dc79C8).
# Anyone can sign with it: local development only.
DEV_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


@dataclass(frozen=True)
class Settings:
    private_key: str = DEV_PRIVATE_KEY
    key_id: str = "local-dev-attestor-1"
    chain_state: Path | None = None  # set: read this mock chain file
    chain_kind: str | None = None  # else connect(): evm | mock
    chain_rpc_url: str | None = None
    chain_contract: str | None = None
    cache_dir: Path = Path.home() / ".dosr" / "attestor-cache"
    max_bundle_bytes: int = 50 * 1024 * 1024
    git_timeout_seconds: float = 30.0
    attestation_ttl_seconds: int = 3600
    providers_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        default = cls()
        return cls(
            private_key=env.get("ATTESTOR_PRIVATE_KEY", default.private_key),
            key_id=env.get("ATTESTOR_KEY_ID", default.key_id),
            chain_state=Path(env["DOSR_CHAIN_STATE"]) if env.get("DOSR_CHAIN_STATE") else None,
            chain_kind=env.get("DOSR_CHAIN") or None,
            chain_rpc_url=env.get("DOSR_RPC_URL") or None,
            chain_contract=env.get("DOSR_CONTRACT") or None,
            cache_dir=Path(env.get("ATTESTOR_CACHE_DIR", default.cache_dir)),
            max_bundle_bytes=int(env.get("ATTESTOR_MAX_BUNDLE_BYTES", default.max_bundle_bytes)),
            providers_file=Path(env["ATTESTOR_PROVIDERS_FILE"]) if env.get("ATTESTOR_PROVIDERS_FILE") else None,
        )

    def chain(self):
        """The chain the attestor reads canonical HEADs and genesis policy hashes from (read
        only: it never sends transactions). EVM when DOSR_CHAIN=evm / DOSR_RPC_URL is set."""
        if self.chain_state is not None:
            return MockChain(self.chain_state)
        return _connect(self.chain_kind, self.chain_rpc_url, self.chain_contract)


@lru_cache(maxsize=8)
def _connect(kind, rpc_url, contract):
    # One connection per chain (keeps the HTTP session and cached chain id across requests).
    return connect(kind, rpc_url=rpc_url, contract=contract)
