# DOSR Attestor API (v0.1, mock)

Contract between the DOSR client and the attestor server. Base URL for local
development: `http://127.0.0.1:8080`. Interactive docs (generated from the code)
are at `/docs`; machine-readable schema at `/openapi.json`.

Status: the server validates formats and makes a mock decision. Bundle contents,
chain state, LLM review and signatures are **not** real yet — see
[README.md](README.md#not-implemented-yet-full-attestor-responsibilities).

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
      "address": "0x1111111111111111111111111111111111111111"
    }
  ]
}
```

## `POST /v1/reviews`

Request a review of a `parent -> candidate` transition.

### Request

`Content-Type: multipart/form-data` with three **required** parts:

| Part | Content | Example |
|---|---|---|
| `request` | Review request JSON | [`examples/wire/1. attestor-request.json`](../../examples/wire/1.%20attestor-request.json) |
| `policy` | The repository's immutable policy JSON | [`examples/repos/hello-python/.dosr/policy.json`](../../examples/repos/hello-python/.dosr/policy.json) |
| `bundle` | Git bundle containing `parent -> candidate` (binary) | — |

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
    "summary": "Rejected: model mock/some-other-model@1 is not approved by the policy"
  },
  "evidence": {
    "canonical_request_hash": "0x…",
    "model_response_hash": "0x…"
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
    "response_hash": "0x…",
    "issued_at": 1790960400,
    "expires_at": 1790964000,
    "attestor_key_id": "local-dev-attestor-1"
  },
  "signature": {
    "scheme": "eip712-secp256k1",
    "signer": "0x1111111111111111111111111111111111111111",
    "value": "0x0000…"
  }
}
```

Field notes:
- `attestation.verifying_contract` is copied from `request.repository.contract`.
- `issued_at` / `expires_at` are Unix seconds; attestations are valid for 1 hour.
- `request_hash` = sha256 of the raw `request` part bytes (not yet JCS-canonicalized).
- `signature.value` is currently 65 zero bytes (placeholder).

#### Mock decision rules

The review is rejected if any of these fail (all failures are listed in `summary`):

1. `(review.provider, review.model, review.model_version)` is in `policy.review.approved_models`
2. This attestor's `key_id` is in `policy.attestors`
3. Both OIDs use `policy.git.object_format` as their prefix
4. `parent_git_oid != candidate_git_oid`
5. `transition.bundle.format == policy.storage.bundle_format`

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
2. **Status codes** — a well-formed but rejected review returns `200` with
   `approved: false` (not 4xx). OK?
3. **Rejected response shape** — we return the full attestation for rejections;
   `examples/wire/3. attestor-response-rejected.json` shows a reduced set. Update the example?
4. **Naming** — request uses `repository.contract`, response uses
   `attestation.verifying_contract`. Unify?
5. **LLM API key** — `docs/API.md` says to send it in a header; which header name?
