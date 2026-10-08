# DOSR Attestor

Python / FastAPI server. **Current status: mock reviewer** — it validates the
request/policy format and makes an approve/reject decision from cheap
request-vs-policy checks. No Git, chain, LLM, or real signature yet.

## Run

```bash
cd services/attestor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn attestor.main:app --port 8080 --reload
```

Interactive API docs: http://127.0.0.1:8080/docs

## Try it

From the repo root, with the server running:

```bash
curl http://127.0.0.1:8080/v1/health

echo "fake bundle" > /tmp/test.bundle
curl -X POST http://127.0.0.1:8080/v1/reviews \
  -F "request=@examples/wire/1. attestor-request.json" \
  -F "policy=@examples/repos/hello-python/.dosr/policy.json" \
  -F "bundle=@/tmp/test.bundle"
```

Using `examples/repos/strict-demo/.dosr/policy.json` instead returns a rejection
(the request's model and this attestor's key are not in that policy).

## Test

```bash
cd services/attestor
pytest
```

## API

See [API.md](API.md) for the full request/response contract, error codes,
mock decision rules, and open questions.

- `GET /v1/health` — liveness.
- `GET /v1/keys` — public attestor key metadata.
- `POST /v1/reviews` — multipart `request` + `policy` + `bundle` → review result.

## Config

| Env var | Default |
|---|---|
| `ATTESTOR_KEY_ID` | `local-dev-attestor-1` |
| `ATTESTOR_ADDRESS` | `0x1111111111111111111111111111111111111111` |

## Not implemented yet (full attestor responsibilities)

1. Read canonical HEAD + policy hash from the contract.
2. Hash and validate the supplied immutable policy.
3. Verify the Git parent/candidate objects from the bundle (and bundle sha256).
4. Derive the diff/context itself; do not trust a client-supplied diff as authoritative.
5. Construct the canonical review prompt.
6. Call the configured model.
7. Hash request/response evidence (currently plain sha256, not JCS-canonicalized).
8. Sign an EIP-712 attestation over the exact transition and decision
   (currently a zero placeholder signature).
