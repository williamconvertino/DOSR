# DOSR Monorepo Scaffold

This scaffold separates the DOSR client, attestation service, smart contracts, shared protocol definitions, and demo repositories.

## Top-level components

- `apps/client/`: user-facing DOSR CLI/application. Creates candidate commits, publishes/fetches repository objects, submits review requests, submits accepted transitions on-chain, and verifies canonical state.
- `services/attestor/`: trusted review/attestation server. Verifies policy + parent/candidate transition, derives review input from Git data, calls the configured LLM, and signs the result.
- `contracts/`: EVM smart contracts that hold the canonical repository head and verify signed attestations.
- `packages/protocol/`: versioned protocol types, canonical serialization rules, and JSON schemas shared by client/server/tests.
- `packages/git-storage/`: Git bundle/object and IPFS adapter code.
- `packages/chain/`: client-side EVM/RPC adapter code.
- `packages/crypto/`: hashing, EIP-712 encoding, and signature verification helpers.
- `examples/repos/`: small DOSR-enabled repositories/configurations for demos and integration tests.
- `examples/wire/`: concrete example protocol payloads.
- `tests/`: unit, integration, and end-to-end tests.
- `fixtures/`: deterministic Git repositories, attestations, and malformed inputs.
- `docs/`: architecture, protocol, threat model, and API documentation.

## Security boundary rule

The client is untrusted. The attestor must independently verify all security-relevant claims in a review request. The contract trusts only immutable repository policy/state plus signatures from approved attestors.
