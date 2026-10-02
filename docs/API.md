# Initial API Shape

## POST /v1/reviews

Use `multipart/form-data` for the prototype:

- `request`: JSON matching `examples/wire/attestor-request.json`
- `policy`: immutable policy JSON (attestor hashes it and checks the on-chain `policyHash`)
- `bundle`: Git bundle for `parent -> candidate`

The provider API key should be sent only over TLS, preferably as a dedicated authorization header or short-lived delegated credential. Do not persist it in logs, request fixtures, attestations, or blockchain data.

## Response

Return the review result, hashes of the canonical model request/response, and a signed attestation. Rejected reviews can be signed for audit/debugging but are never accepted by the contract.
