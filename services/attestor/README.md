# DOSR Attestor

Suggested API:

- `POST /v1/reviews` — multipart request containing `request.json` and a Git bundle (or a verified content-addressed reference to it).
- `GET /v1/health`
- `GET /v1/keys` — public attestor key metadata.

The attestor should:
1. Read canonical HEAD + policy hash from the contract.
2. Hash and validate the supplied immutable policy.
3. Verify the Git parent/candidate objects from the bundle.
4. Derive the diff/context itself; do not trust a client-supplied diff as authoritative.
5. Construct the canonical review prompt.
6. Call the configured model.
7. Hash request/response evidence.
8. Sign an EIP-712 attestation over the exact transition and decision.
