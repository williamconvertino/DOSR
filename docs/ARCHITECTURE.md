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
