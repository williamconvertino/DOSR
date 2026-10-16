# DOSR Attestor API (v0.1)

Contract between the DOSR client and the attestor server. Base URL for local
development: `http://127.0.0.1:8080`. Interactive docs (generated from the code)
are at `/docs`; machine-readable schema at `/openapi.json`.

Status: policy, chain state (mock chain), Git bundle, prompt building, model call
(`mock` or any OpenAI-compatible provider; `zhipu` built in), output parsing and EIP-712 signatures are real —
see [README.md](README.md#not-implemented-yet).

---

## `GET /v1/health`

Liveness check.

```json
{ "status": "ok", "protocol_version": "0.1" }
```

## `GET /v1/keys`

Public metadata for the key(s) this attestor signs with. Clients can match
`key_id` / `address` against `policy.attestors`.

```json
{
  "keys": [
    {
      "key_id": "local-dev-attestor-1",
      "scheme": "eip712-secp256k1",
      "address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    }
  ]
}
```

## `GET /v1/repos/{repo_id}/commits/{git_oid}`

Whether the attestor already holds this commit (from an earlier verified bundle).
Clients call it before sending a thin bundle based on `git_oid`; if `false`, send a
full bundle.

```json
{ "present": true }
```

## `POST /v1/reviews`

Request a review of a `parent -> candidate` transition.

### Request

Header `X-DOSR-Provider-Key`: the contributor's API key for the review model's
provider (required for every provider except `mock`). Used for this one model call;
never stored, logged, hashed, or returned.

`Content-Type: multipart/form-data` with three **required** parts:

| Part | Content | Example |
|---|---|---|
| `request` | Review request JSON | [`examples/wire/1. attestor-request.json`](../../examples/wire/1.%20attestor-request.json) |
| `policy` | The repository's immutable policy JSON | [`examples/repos/hello-python/.dosr/policy.json`](../../examples/repos/hello-python/.dosr/policy.json) |
| `bundle` | Git bundle with the candidate and its history back to `parent` (binary). Full, or thin if the attestor has the parent. | — |

```bash
curl -X POST http://127.0.0.1:8080/v1/reviews \
  -F "request=@examples/wire/1. attestor-request.json" \
  -F "policy=@examples/repos/hello-python/.dosr/policy.json" \
  -F "bundle=@change.bundle"
```

#### `request` fields

**Unknown fields are rejected** (422). All fields below are required unless marked optional.

| Field | Type / format |
|---|---|
| `protocol_version` | `"0.1"` exactly |
| `request_id` | non-empty string |
| `repository.repo_id` | `0x` + 64 hex |
| `repository.chain_id` | integer |
| `repository.contract` | `0x` + 40 hex (address) |
| `repository.policy_hash` | `0x` + 64 hex |
| `transition.parent_git_oid` | `sha1:` + 40 lowercase hex, or `sha256:` + 64 lowercase hex |
| `transition.candidate_git_oid` | same as above |
| `transition.bundle.format` | `"git-bundle-v1"` |
| `transition.bundle.sha256` | `sha256:` + 64 lowercase hex |
| `transition.bundle.ipfs_cid` | string, **optional** |
| `review.provider` | string |
| `review.model` | string |
| `review.model_version` | string |
| `client.client_version` | string |

#### `policy` fields

Must match the shape of the files under `examples/repos/*/.dosr/policy.json`.
Unknown fields are rejected. Notable constraints: `protocol_version` is `"0.1"`;
`git.object_format` is `sha1` or `sha256`; `review.approved_models` and
`attestors` must be non-empty; `attestors[].scheme` is `eip712-secp256k1`;
`storage.bundle_format` is `git-bundle-v1`.

### Response

| Status | Meaning |
|---|---|
| `200` | Request was well-formed and reviewed. Check `result.approved` — it can be `true` **or** `false`. |
| `400` | A part is not valid JSON, or `bundle` is empty. `detail` is a message string. |
| `409` | Thin bundle whose prerequisite commit(s) the attestor does not have. `detail = {"code": "missing_prerequisites", "message", "missing": [oid…]}`. Resend a full bundle. |
| `413` | `bundle` is larger than `ATTESTOR_MAX_BUNDLE_BYTES` (50 MB). |
| `400` | (also) The model's provider needs `X-DOSR-Provider-Key` and none was sent. |
| `502` | The model provider could not be called (network, bad key, quota). Nothing is signed; retry. |
| `422` | A part failed schema validation, or a part is missing. See below. |

#### 200 body

Same shape for approve and reject; matches
[`examples/wire/3. attestor-response-approved.json`](../../examples/wire/3.%20attestor-response-approved.json).

```json
{
  "protocol_version": "0.1",
  "request_id": "01J9DOSRDEMO00000000000001",
  "result": {
    "approved": false,
    "summary": "Rejected: model mock/some-other-model@1 is not approved by the policy",
    "issues": ["model mock/some-other-model@1 is not approved by the policy"]
  },
  "evidence": {
    "canonical_request_hash": "0x…",
    "model_response_hash": "0x…",
    "checks": [
      { "name": "model_is_approved", "ok": false, "detail": "model mock/some-other-model@1 is not approved by the policy" },
      …
    ],
    "derived_review_input": null
  },
  "attestation": {
    "repo_id": "0x…",
    "chain_id": 31337,
    "verifying_contract": "0x…",
    "policy_hash": "0x…",
    "parent_git_oid": "sha1:…",
    "candidate_git_oid": "sha1:…",
    "bundle_sha256": "sha256:…",
    "bundle_cid": "bafy…",
    "provider": "mock",
    "model": "some-other-model",
    "model_version": "1",
    "approved": false,
    "request_hash": "0x…",
    "review_input_hash": "0x0000…",
    "response_hash": "0x0000…",
    "issued_at": 1790960400,
    "expires_at": 1790964000,
    "attestor_key_id": "local-dev-attestor-1"
  },
  "signature": {
    "scheme": "eip712-secp256k1",
    "signer": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "value": "0x…65 bytes (r, s, v)…"
  }
}
```

Field notes:
- `attestation.verifying_contract` is copied from `request.repository.contract`.
- `issued_at` / `expires_at` are Unix seconds; attestations are valid for 1 hour.
- `result` is the parsed model decision `{approved, summary, issues}`, or a rejection
  listing the failed checks as `issues`.
- Hashes ([docs/HASHING.md](../../docs/HASHING.md)): `request_hash` = sha256 of the
  canonical JSON of the `request` part (whitespace / key order do not matter);
  `review_input_hash` = sha256 of the prompt the attestor built and sent to the model;
  `response_hash` = sha256 of the model's raw output before parsing. Both of the
  latter are 32 zero bytes when the model was not called.
- `evidence.derived_review_input` (only once the bundle was opened):
  `pr_hash`, `commit_count`, `changed_files`, `changed_file_count`, `patch_bytes`,
  `patch_sha256`, all read from the bundle, not from the client. `pr_hash` equals the
  client's own PR hash when both are honest (same `dosr_git.pr_inputs` code).
- Rejections are signed too, with the full attestation and `approved: false`.

#### Signature

EIP-712 over the attestation (`packages/protocol/dosr_protocol/eip712.py`):

- Domain: `name "DOSR"`, `version "0.1"`, `chainId`, `verifyingContract`.
- Struct `Attestation`: every `attestation` field except the domain ones, in the
  order of `ATTESTATION_FIELDS`. Hashes and OIDs are `bytes32`; a SHA-1 Git OID is
  right-padded with zeros (Solidity `bytes32(bytes20(x))`).
- All content hashes are SHA-256; Keccak-256 appears only inside EIP-712's own
  struct hashing (what `ecrecover` checks).

Verify with `dosr_protocol.eip712.recover_signer(attestation, signature.value)`; it
must equal `signature.signer`, which must be listed for `attestor_key_id` in the policy.
Other implementations: reproduce `examples/wire/6. eip712-test-vector.json`.

#### Checks

Run in this order (names as in `evidence.checks`). The bundle is opened only if
everything in 1–2 passed; the reviewer is called only if everything passed. Any
failure rejects, and every failure is listed in `summary`.

1. Request vs policy: `policy_hash_matches` (sha256 of canonical policy JSON),
   `model_is_approved`, `provider_is_supported` (this attestor has a backend for it), `attestor_is_listed`, `attestor_address_matches`,
   `prompt_template_is_approved`, `decision_schema_is_known`, `parent_git_oid_format`, `candidate_git_oid_format`,
   `candidate_differs_from_parent`, `bundle_format_matches`, `bundle_hash_matches`
   (sha256 of the received bytes).
2. Chain: `chain_matches` (request's chain id / contract = the chain this attestor
   reads), `repository_exists`, `policy_hash_matches_genesis`, `parent_is_canonical_head`.
3. Bundle: `bundle_contains_transition` (bundle unpacks; candidate is a commit that
   descends from `parent`, all objects present), `protected_paths_unchanged`
   (`.dosr/policy.json`, `.dosr/repo.json`), `policy_file_matches` (candidate's
   policy file hashes to the policy hash), `within_max_changed_files`,
   `within_max_patch_bytes` (same diff flags as the client's PR builder).
4. Model: `model_output_is_valid` (raw output parses under `decision_schema`).

#### 422 body

```json
{
  "detail": {
    "field": "request",
    "errors": [
      { "type": "missing", "loc": ["transition"], "msg": "Field required", "input": { … } }
    ]
  }
}
```

`detail.field` is `"request"` or `"policy"`. If a whole multipart part is missing,
FastAPI returns its default 422 format instead (`detail` is a list).

---

## Open questions (to confirm with the client side)

1. **Strict fields** — unknown fields are rejected. Keep, or ignore extras?
2. **Naming** — request uses `repository.contract`, response uses
   `attestation.verifying_contract`. Unify?
3. **Bundle source** — once IPFS is in, should the attestor fetch the parent from the
   CID instead of answering 409 for unknown thin bundles?
