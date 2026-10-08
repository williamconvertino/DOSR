"""File-backed stand-in for the DOSR repository contract.

MOCK ONLY: there is no EVM, no gas, and no signature verification (the attestor
still returns a placeholder signature). It exists so the client has a canonical
HEAD to build PRs on top of, and it enforces the same deterministic checks the
real contract will (contracts/README.md):

- repository registered with an immutable policy hash
- attestation signer/key listed in the genesis policy
- attestation approved and not expired
- attested parent == current HEAD (first registered wins)

The method names mirror the planned contract API so this class can be swapped
for an RPC-backed adapter later without touching the client pipeline.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from dosr_protocol.canonical import canonical_json, policy_hash, sha256_0x


class ChainError(RuntimeError):
    pass


def default_state_path() -> Path:
    env = os.environ.get("DOSR_CHAIN_STATE")
    if env:
        return Path(env)
    return Path.home() / ".dosr" / "mock-chain" / "state.json"


class MockChain:
    chain_id = 31337
    contract = "0x4444444444444444444444444444444444444444"

    def __init__(self, state_path=None):
        self.state_path = Path(state_path) if state_path else default_state_path()

    # ---------- storage ----------

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"chain_id": self.chain_id, "contract": self.contract, "block": 0, "repos": {}}
        return json.loads(self.state_path.read_text("utf-8"))

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self.state_path)

    # ---------- reads (free on a real chain) ----------

    def get_repository(self, repo_id: str) -> dict | None:
        return self._load()["repos"].get(repo_id)

    def get_head(self, repo_id: str) -> str:
        repo = self.get_repository(repo_id)
        if not repo:
            raise ChainError(f"repository {repo_id[:18]}… is not registered on the chain")
        return repo["canonical_head"]

    def list_repositories(self) -> dict:
        return self._load()["repos"]

    # ---------- writes (transactions on a real chain) ----------

    def register_repository(self, *, repo_id: str, name: str, policy: dict, genesis_git_oid: str,
                            object_format: str) -> dict:
        state = self._load()
        if repo_id in state["repos"]:
            raise ChainError(f"repository {repo_id[:18]}… is already registered")
        state["block"] += 1
        state["repos"][repo_id] = {
            "repo_id": repo_id,
            "name": name,
            "policy_hash": policy_hash(policy),
            # A real contract would store only the attestor set; we keep the policy
            # so the mock can check signers against it.
            "attestors": policy["attestors"],
            "git_object_format": object_format,
            "genesis_git_oid": genesis_git_oid,
            "canonical_head": genesis_git_oid,
            "accepted_transition_count": 0,
            "registered_block": state["block"],
            "registered_at": int(time.time()),
            "events": [],
        }
        self._save(state)
        return state["repos"][repo_id]

    def accept_transition(self, response: dict, now: int | None = None) -> dict:
        """Mirror of `acceptTransition` (examples/wire/4.*). Returns the event."""
        now = int(time.time()) if now is None else now
        att, sig = response["attestation"], response["signature"]
        state = self._load()
        repo = state["repos"].get(att.get("repo_id"))

        def fail(msg):
            raise ChainError(f"transition rejected by contract: {msg}")

        if repo is None:
            fail("unknown repository")
        if att.get("chain_id") != state["chain_id"] or (att.get("verifying_contract") or "").lower() != state["contract"].lower():
            fail("attestation is for a different chain/contract (domain mismatch)")
        if att.get("policy_hash") != repo["policy_hash"]:
            fail("policy hash does not match the genesis policy")
        signers = {(a["key_id"], a["address"].lower()) for a in repo["attestors"]}
        if (att.get("attestor_key_id"), (sig.get("signer") or "").lower()) not in signers:
            fail("attestor is not approved by the repository policy")
        # TODO: recover the EIP-712 signer from sig["value"] once the attestor signs.
        if not att.get("approved"):
            fail("attestation is not an approval")
        if att.get("expires_at", 0) <= now:
            fail("attestation has expired")
        if att.get("parent_git_oid") != repo["canonical_head"]:
            fail(
                f"stale parent: attested parent {att.get('parent_git_oid')} != HEAD {repo['canonical_head']}"
            )

        state["block"] += 1
        event = {
            "block": state["block"],
            "repo_id": repo["repo_id"],
            "parent_git_oid": att["parent_git_oid"],
            "candidate_git_oid": att["candidate_git_oid"],
            "attestor": sig.get("signer"),
            "attestation_hash": sha256_0x(canonical_json(att)),
            "bundle_cid": att.get("bundle_cid"),
            "timestamp": now,
        }
        repo["canonical_head"] = att["candidate_git_oid"]
        repo["accepted_transition_count"] += 1
        repo["events"].append(event)
        self._save(state)
        return event
