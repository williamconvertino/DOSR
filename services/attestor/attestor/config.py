"""Attestor settings, read from the environment.

    ATTESTOR_PRIVATE_KEY   secp256k1 signing key (default: public dev key below)
    ATTESTOR_KEY_ID        key id repositories list in policy.attestors
    DOSR_CHAIN_STATE       mock chain state file (default: MockChain's default)
    ATTESTOR_CACHE_DIR     per-repository Git object cache (lets clients send thin bundles)
    ATTESTOR_MAX_BUNDLE_BYTES
    ATTESTOR_PROVIDERS_FILE  JSON of extra OpenAI-compatible model providers (see reviewers.py)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dosr_chain import MockChain

# Hardhat/Anvil's well-known test account #1 (address 0x70997970C51812dc3A010C7d01b50e0d17dc79C8).
# Anyone can sign with it: local development only.
DEV_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


@dataclass(frozen=True)
class Settings:
    private_key: str = DEV_PRIVATE_KEY
    key_id: str = "local-dev-attestor-1"
    chain_state: Path | None = None
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
            cache_dir=Path(env.get("ATTESTOR_CACHE_DIR", default.cache_dir)),
            max_bundle_bytes=int(env.get("ATTESTOR_MAX_BUNDLE_BYTES", default.max_bundle_bytes)),
            providers_file=Path(env["ATTESTOR_PROVIDERS_FILE"]) if env.get("ATTESTOR_PROVIDERS_FILE") else None,
        )

    def chain(self) -> MockChain:
        # TODO: swap for an RPC-backed reader once the contract is deployed.
        return MockChain(self.chain_state)
