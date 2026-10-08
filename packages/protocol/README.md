# DOSR Protocol Package

Keep all wire types here and version them explicitly. Use deterministic serialization before hashing/signing. For JSON, use RFC 8785 JSON Canonicalization Scheme (JCS), or replace JSON signatures entirely with a typed binary encoding later.

Git object IDs should be algorithm-qualified strings (for example `sha1:<hex>`), so the protocol does not silently assume SHA-1 forever.

## Python package (`dosr_protocol`, v0.1)

Dependency-free so the client, attestor, and tests can all import it.

- `canonical.py` — JCS-compatible `canonical_json` (floats rejected), `policy_hash`, sha256 helpers, OID qualify/unqualify, ULID `new_request_id`, prototype `derive_repo_id`.
- `policy.py` — `standard`/`strict` presets (identical to `examples/repos/*/.dosr/policy.json`; a test enforces this), `build_policy` overrides, light validation mirroring the attestor's strict schema.
- `wire.py` — `build_review_request` (must stay in sync with `services/attestor/attestor/models.py`) and client-side `verify_response`.
- `pr_format.py` — PR templates keyed by `policy.review.prompt_template` (`standard-code-review-v1`, `security-sensitive-review-v1`), `build_pr`, `render_markdown`, limit checks. Pure functions so the attestor can later rebuild the PR from the bundle itself.
