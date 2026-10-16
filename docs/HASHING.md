# Hashing, Canonical Serialization, and Signatures (protocol v0.1)

What gets hashed, how it is serialized first, and what the attestor signs. Every
implementation (Python client and attestor, the Solidity contract, any third-party
attestor) must follow this exactly, or the same data produces different hashes.

Code: `packages/protocol/dosr_protocol/canonical.py` (serialization),
`eip712.py` (signature), `decision.py` (prompt and model output).
Test vector: [`examples/wire/6. eip712-test-vector.json`](../examples/wire/6.%20eip712-test-vector.json).

## Rule of thumb

- **SHA-256 for every content hash** (policy, bundle, request, review input, model output).
- **Keccak-256 only inside EIP-712**, because the EVM's `ecrecover` checks a Keccak
  digest. To EIP-712 the SHA-256 values are opaque `bytes32` fields; the contract
  never recomputes them, it only compares them.
- **Git OIDs are not rehashed.** They are already content hashes (SHA-1 or SHA-256,
  per `policy.git.object_format`) and are the canonical repository identifiers. An
  IPFS CID is only a transport pointer and is never treated as the commit identity.

## What is hashed

| Value | Input | Serialization | Format |
|---|---|---|---|
| `policy_hash` | the whole `.dosr/policy.json` object | `canonical_json` | `0x` + 64 hex |
| `bundle.sha256` | the raw Git bundle bytes as uploaded | none (raw bytes) | `sha256:` + 64 hex |
| `request_hash` | the whole `request` part of `POST /v1/reviews` | `canonical_json` | `0x` + 64 hex |
| `review_input_hash` | the exact prompt text sent to the model | UTF-8 bytes of the string | `0x` + 64 hex |
| `response_hash` | the model's raw output (the reply's `content`), **before** parsing | UTF-8 bytes of the string | `0x` + 64 hex |
| `pr_hash` (evidence, not signed) | the PR object from `pr_format.build_pr`, without `pr_hash` | `canonical_json` | `0x` + 64 hex |
| `attestation_hash` (mock chain event) | the attestation object | `canonical_json` | `0x` + 64 hex |

If the model was never called (a deterministic check failed first),
`review_input_hash` and `response_hash` are 32 zero bytes (`0x00…00`).

The prompt is built by the attestor from the Git bundle, never taken from the client:
`decision.build_prompt(template, pr_format.render_markdown(pr), decision_schema)`,
where `pr` comes from `dosr_git.pr_inputs` + `pr_format.build_pr` (the same code the
client uses, so both sides get the same `pr_hash`). It is sent as the single user
message; the other call parameters are fixed per backend in
`services/attestor/attestor/reviewers.py` (for OpenAI-compatible providers:
temperature 0, JSON-object output, plus the provider's configured extra fields, e.g.
thinking disabled for GLM), so the prompt hash plus the model identity determine the
call. The contributor's API key is never part of any hash.

## Canonical JSON (`canonical_json`)

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

- Object keys sorted, no whitespace, UTF-8, non-ASCII characters written as-is.
- Escapes: `\"`, `\\`, `\b \f \n \r \t`, other control characters as `\u00xx`
  (lowercase hex). Nothing else is escaped.
- So `{"b":2,"a":1}` and `{ "a": 1, "b": 2 }` hash the same, and a client may
  pretty-print what it sends.

### Relation to RFC 8785 (JCS)

This is **not a full JCS implementation**. It produces byte-identical output to JCS
for the inputs it accepts, and it raises `TypeError` for every input where Python
and JCS would disagree:

| Where Python and JCS differ | Python's `json.dumps` | JCS | `canonical_json` |
|---|---|---|---|
| Floats | `repr`-style (`1e-07`, `100.0`) | ECMAScript number formatting (`1e-7`, `100`) | rejects all floats |
| Integers beyond 2^53 − 1 | exact digits | read as IEEE-754 doubles, so precision is lost | rejects `\|n\| > 2^53 − 1` |
| Key order with non-ASCII keys | by Unicode code point | by UTF-16 code unit (differs above U+FFFF) | rejects non-ASCII keys |

The escaping rules for strings, and the output for ASCII keys, booleans, `null`, and
safe integers, are identical. All v0.1 payloads use ASCII keys, integers below 2^53
(chain id, Unix timestamps, size limits), and no floats.

A non-Python implementation can use any RFC 8785 library and get the same bytes, as
long as it rejects (or never produces) the inputs in the table above.

## Signature (EIP-712)

The attestor signs the `attestation` object with EIP-712 (secp256k1, 65-byte
`r ‖ s ‖ v` signature).

- **Domain**: `EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)`
  with `name = "DOSR"`, `version = "0.1"`, and `chainId` / `verifyingContract` from
  the attestation. This binds the signature to one chain and one contract.
- **Struct**: `Attestation(...)`, field order and types in `eip712.ATTESTATION_FIELDS`
  (repo id, policy hash, parent and candidate OIDs, bundle sha256 and CID, provider,
  model, model version, approved, request / review input / response hashes,
  issued / expires times, attestor key id).
- **Encoding of values**:
  - `0x…` and `sha256:…` hashes become `bytes32`.
  - A SHA-1 Git OID (20 bytes) is **right-padded** with 12 zero bytes, matching
    Solidity's `bytes32(bytes20(x))`. A SHA-256 Git OID fills all 32 bytes.
  - A missing `bundle_cid` is signed as the empty string.
  - Times are `uint64` Unix seconds.

Every field of the struct is bound: changing any one of them, or the domain, makes
the signature recover to a different address. `packages/protocol/tests` checks this
field by field.

### Verifying (for the contract and other implementations)

1. Recompute `digest = keccak256(0x1901 ‖ domainSeparator ‖ structHash)` from the
   attestation fields.
2. `ecrecover(digest, v, r, s)` must equal the address that the repository policy
   lists for `attestation.attestor_key_id`.
3. Then check `approved`, `expires_at > now`, `policy_hash` equals the genesis policy
   hash, and `parent_git_oid` equals the current HEAD.

The test vector lists the type strings, type hash, domain separator, struct hash,
digest, and signature for one fixed attestation. A Solidity test should reproduce
each of them. Regenerate the vector whenever `eip712.py` changes.

## Model output (`decision_schema`)

`dosr-review-decision-v1` (`decision.py`): the raw output must be exactly one JSON
object `{"approved": bool, "summary": str (1..2000 chars), "issues": [str (<= 500 chars), ...] (<= 50)}`
with no other keys. A single surrounding Markdown code fence (```` ``` ```` or
```` ```json ````) is the only text accepted around it. Anything else is a rejection,
whatever the model meant. `response_hash` is taken before this parsing, so the
signature also commits to exactly what the model returned.
