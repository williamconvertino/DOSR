# DOSR Architecture

## Trust boundaries

### Client / Git / IPFS
Untrusted orchestration and data distribution. The client creates Git commits and bundles, publishes them, asks an attestor for review, and submits approved transitions to the chain.

### Attestor / LLM
Trusted semantic gate. It independently validates the policy and candidate transition, constructs the prompt from verified Git objects, calls an approved model, and signs the exact result.

### EVM
Canonical state/order. It performs cheap deterministic checks only: immutable policy, approved attestor, signature validity, approval, expiration/domain separation, and `parent == HEAD`.

## Canonical data rule

The Git commit OID identifies repository content/history. An IPFS CID is a transport/distribution pointer to a bundle containing those Git objects. Never treat the CID alone as the canonical repository version.

## Recommended signed domain

Implemented in v0.1; the exact fields, encodings, and hash rules are in
[HASHING.md](HASHING.md).

Use EIP-712 with at least:
- protocol version
- chain ID
- verifying contract
- repository ID
- immutable policy hash
- parent Git OID
- candidate Git OID
- bundle digest (and optionally CID)
- provider/model/version
- approval decision
- request hash
- response hash
- issued/expiry time
- attestor key ID

## Git OIDs on-chain

For protocol v0.1, record the repository Git object format at genesis. If using SHA-1, represent the 20-byte Git OID in a fixed 32-byte field by zero-padding and verify the exact conversion in shared protocol code. Do not hash the IPFS CID and treat it as the canonical commit identifier; Git OID remains canonical.

## Canonical state and conflicts (implemented)

The chain (`contracts/src/DOSRRegistry.sol`) is the single source of truth for which
commit is canonical. Clients and attestors read it; only approved, correctly signed
transitions whose parent is the current HEAD change it.

- **Registration:** genesis commit, policy hash and the policy's attestor keys are stored
  once and never change.
- **Racing PRs:** the attestor can approve two PRs built on the same HEAD. The first
  transaction mined moves HEAD; the other reverts with `StaleParent`, even inside the same
  block. The losing client syncs, rebases and resubmits; its new commit is reviewed again.
- **Data availability:** after landing, the client publishes a Git bundle of HEAD to IPFS
  and records its CID on-chain (`publishSnapshot`). Any client can then clone or sync from
  the chain alone; the bundle is checked against the on-chain genesis, policy hash and
  history before use.
- **Verification:** `dosr verify` / the GUI's Chain tab checks a clone against the chain:
  genesis, policy hash, registered attestors vs the policy, a linear history whose every
  signed attestation (rebuilt from transaction calldata) recovers to a registered
  attestor and matches the stored digest, and whether the local branch is in sync,
  ahead, behind or diverged.

The per-device mock chain (`MockChain`) remains for tests and offline use; with it,
devices agree only through share codes (trust on first use) and can fork.
