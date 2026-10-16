# DOSR Protocol Package

Keep all wire types here and version them explicitly. Hashing, canonical JSON, and the signed attestation are specified in [docs/HASHING.md](../../docs/HASHING.md).

Git object IDs should be algorithm-qualified strings (for example `sha1:<hex>`), so the protocol does not silently assume SHA-1 forever.

## Python package (`dosr_protocol`, v0.1)

Dependency-free so the client, attestor, and tests can all import it (`eip712.recover_signer` needs `eth-account`, imported lazily).

- `canonical.py` — `canonical_json` (equals RFC 8785 JCS for what it accepts; rejects floats, integers beyond 2^53 − 1, and non-ASCII keys), `policy_hash`, sha256 helpers, OID qualify/unqualify, ULID `new_request_id`, prototype `derive_repo_id`.
- `policy.py` — `standard`/`strict` presets (identical to `examples/repos/*/.dosr/policy.json`; a test enforces this), `build_policy` overrides, light validation mirroring the attestor's strict schema.
- `wire.py` — `build_review_request` (must stay in sync with `services/attestor/attestor/models.py`) and client-side `verify_response`.
- `pr_format.py` — PR templates keyed by `policy.review.prompt_template` (`standard-code-review-v1`, `security-sensitive-review-v1`), `build_pr`, `render_markdown`, limit checks. Pure functions; the attestor rebuilds the PR from the bundle with them.
- `decision.py` — review prompt (`build_prompt`) and the `dosr-review-decision-v1` model-output parser (`parse_decision`).
- `eip712.py` — typed data for the signed attestation, `recover_signer`. Test vector: `examples/wire/6. eip712-test-vector.json`.
