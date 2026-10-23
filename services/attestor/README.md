# DOSR Attestor

Python / FastAPI server. It independently verifies a proposed `parent -> candidate`
transition (policy, chain state, Git bundle), runs the reviewer, and signs an EIP-712
attestation over the result. Model backends (`attestor/reviewers.py`): `mock`
(approves anything passing the checks), plus built-in `openai`, `anthropic` (Claude,
Messages API), `gemini` (Google, OpenAI-compatible endpoint) and `zhipu` (GLM). Other
OpenAI-compatible providers (DeepSeek, Qwen, ...) are one entry in `BUILTIN_PROVIDERS` or in
the JSON file named by `ATTESTOR_PROVIDERS_FILE`. Models are called with the contributor's
own key from the `X-DOSR-Provider-Key` header, which is never stored.

Policies name models as `provider/model@version`. For providers with dated snapshots the
version is appended to get the exact model id called (`openai/gpt-4o@2024-08-06` →
`gpt-4o-2024-08-06`, `anthropic/claude-sonnet-4-5@20250929` → `claude-sonnet-4-5-20250929`);
`stable` (or Zhipu's `api-v4`) means "send the model name as-is" for ids the provider already
pins (`gemini-2.5-pro`, `claude-opus-5-5`). The list of common models lives in
`packages/protocol/dosr_protocol/models.py`.

Fixed call parameters (part of what the attestor vouches for): one user message with the
prompt, temperature 0 and JSON-object output where the API supports them. Exceptions, set per
model prefix in `BUILTIN_PROVIDERS`: OpenAI reasoning models (`gpt-5*`, `o1/o3/o4*`) take no
temperature and use `reasoning_effort: low`; Gemini 2.5+/3 use `reasoning_effort: low`; Claude
has no JSON mode (the prompt asks for one JSON object), and Claude 5 models use the default
temperature.

## Run

The attestor imports the shared packages (`dosr_protocol`, `dosr_git`, `dosr_chain`). Easiest is
the demo venv, which installs everything:

```bash
python demo/setup_env.py
demo/.venv/bin/python demo/demo.py attestor     # :8080, reads demo/workspace/.chain
```

Or by hand:

```bash
pip install -r services/attestor/requirements.txt -e packages/protocol -e packages/git-storage -e packages/chain
cd services/attestor
uvicorn attestor.main:app --port 8080 --reload
```

Interactive API docs: http://127.0.0.1:8080/docs

The attestor reads canonical HEAD and the genesis policy hash from the mock chain,
so it must point at the **same** state file as the client (`DOSR_CHAIN_STATE`).
`demo.py` does this for you.

## Test

```bash
cd services/attestor
../../demo/.venv/bin/python -m pytest
```

`tests/test_api.py` builds real Git repositories, bundles and a mock chain per test,
and covers each check below plus signature recovery and chain acceptance.
`apps/client/tests` run the real client pipeline against this app in-process.

## API

See [API.md](API.md) for the full request/response contract, checks, signature
format, and error codes.

- `GET /v1/health` — liveness.
- `GET /v1/keys` — key id + signing address.
- `GET /v1/repos/{repo_id}/commits/{git_oid}` — does the attestor have this commit (thin bundles)?
- `POST /v1/reviews` — multipart `request` + `policy` + `bundle` → signed review.

## What it verifies

1. **Policy**: sha256 of the canonical policy JSON equals the request's policy hash
   *and* the hash registered at genesis; model, prompt template, and this attestor's
   key id + address are in the policy.
2. **Chain**: repository is registered; declared parent is the current canonical HEAD.
3. **Bundle**: sha256 of the received bytes matches; it unpacks (in a temp repo with
   no system/global Git config) to a candidate commit descending from the parent;
   `.dosr/policy.json` / `.dosr/repo.json` unchanged; changed-file and patch-size
   limits, computed from the unpacked objects.
4. **Review**: builds the prompt itself from the bundle (same PR code as the client),
   calls the reviewer, hashes its raw output, then parses it with the policy's
   `decision_schema`. Unparseable output is a rejection.
5. **Signature**: EIP-712 over all of it, including `review_input_hash` (the prompt)
   and `response_hash` (raw model output). Checked by the mock chain and by the
   client's `verify_response`. Hash rules: [docs/HASHING.md](../../docs/HASHING.md).

Objects from verified bundles are cached per repository (`ATTESTOR_CACHE_DIR`) so
clients can send thin bundles afterwards; a thin bundle the attestor cannot open
gets `409 missing_prerequisites`.

## Config

| Env var | Default |
|---|---|
| `ATTESTOR_PRIVATE_KEY` | Hardhat test account #1 (`0x7099…79C8`); **public, dev only** |
| `ATTESTOR_KEY_ID` | `local-dev-attestor-1` |
| `DOSR_CHAIN_STATE` | `~/.dosr/mock-chain/state.json` |
| `ATTESTOR_CACHE_DIR` | `~/.dosr/attestor-cache` |
| `ATTESTOR_MAX_BUNDLE_BYTES` | `52428800` (50 MB) |
| `ATTESTOR_PROVIDERS_FILE` | none. JSON `{"<provider>": {"url": ..., "api": "chat-completions" \| "anthropic-messages", "extra_body": {...}, "model_extra": {"<model prefix>": {...}}, "snapshot": "<regex>", "token_param": "max_tokens"}}` adds or overrides providers (a `null` value in `extra_body`/`model_extra` removes that field) |

## Not implemented yet

1. Providers whose API is neither OpenAI-compatible nor Anthropic's Messages API need their own
   backend class in `reviewers.py`. The OpenAI/Claude/Gemini backends are tested against fake
   HTTP transports only; they have not been run against the live APIs yet.
2. `minimum_approvals > 1` (several models / attestors).
3. Per-client rate limiting, similar-request cache, prompt-injection detection.
4. Read the chain over RPC instead of the mock state file; fetch missing parents
   from IPFS instead of answering 409.
