# Review notes: client PR pipeline + weekly demo (Will, week of 10/9)

Branch: `will/client-pr-pipeline-demo` (local only, **not pushed**).
Scope: Will's weekly TODO and the "Weekly DEMO Goals" in `weekly-todos/10-9-2026.MD`.

---

## TL;DR: how to run it

```bash
python demo/setup_env.py                              # once: creates demo/.venv
demo\.venv\Scripts\python.exe demo/demo.py up         # attestor + GUI (opens http://127.0.0.1:8765)
demo\.venv\Scripts\python.exe demo/demo.py run --reset --start-attestor   # scripted CLI demo
```

(macOS/Linux: `demo/.venv/bin/python`.) The full walkthrough is in [`demo/README.md`](demo/README.md).

---

## Status against the weekly goals

| Goal | Status | Where |
|---|---|---|
| Basic PR pipeline | ✅ 10-step pipeline: preflight → commit → canonical parent → bundle → PR → request → upload → await → verify → register | `apps/client/dosr_client/pipeline.py` |
| API calls to the verifier (send PR, receive response) | ✅ `POST /v1/reviews` multipart, plus `/v1/health` and `/v1/keys`; 400/422 errors are turned into readable messages | `apps/client/dosr_client/attestor.py` |
| Simple GUI showing each step, latency, errors | ✅ Local web GUI: live step list, per-step latency, elapsed and remaining estimate, decision card, latency bars, errors and warnings | `apps/client/dosr_client/gui/` |
| Demo codebase: 2-3 mock projects + fake clients | ✅ 3 seed projects (`calculator-py`, `todo-js`, `secure-vault`), 4 profiles (Alice, Bob, Carol, Mallory) | `demo/projects/`, `demo/clients/` |
| Launch one client app | ✅ `dosr` CLI + `dosr gui` | `apps/client` |
| Launch DOSR server on the same machine | ✅ `demo.py attestor` / `demo.py up` (uses Yuhan's server unchanged) | `demo/demo.py` |
| GUI + well-formatted CLI for everything below | ✅ both front-ends use the same library (rich CLI, web GUI) | |
| Create a repo with initial config | ✅ `dosr init` / GUI "+ New": preset (`standard`/`strict`), models, template, limits, seed project; genesis commit; registered on the mock chain | `repo_ops.py` |
| Edit code + track with git | ✅ real git underneath; GUI has a file editor, diff view, and discard; CLI has `status`/`diff`/`commit` | |
| Submit changes to the DOSR app | ✅ `dosr submit -m` / GUI "Submit to DOSR" | |
| Bundle changes + relevant files per config | ✅ real `git bundle` (full or thin) + sha256. The PR template picks context files (README, dependency manifests for the strict template) | `packages/git-storage`, `pr_format.py` |
| Generate a formatted PR per config | ✅ `policy.review.prompt_template` selects a template: diff context lines, full changed files, context globs, review instructions/checklist. Outputs `pr.json` + `pr.md`. Enforces `max_changed_files` / `max_patch_bytes` | `packages/protocol/dosr_protocol/pr_format.py` |
| Send PR to server; server approves or rejects | ✅ approve and reject both shown in the demo | |
| Progress display (stage, elapsed/remaining) | ✅ CLI uses rich `Live` (falls back to line logs when piped); GUI polls every 120 ms. ETA comes from a per-repo moving average of past step timings | `render.py`, `timings.py` |
| Final decision + per-step latency + errors | ✅ in both CLI and GUI, and saved per submission (`dosr history` / `dosr show`, GUI History tab) | |

**Scripted demo result** (run locally, cold start):

| Scenario | Client | Expected | Actual |
|---|---|---|---|
| Create calculator-py | Alice | created | created |
| Add power()/modulo() (thin bundle) | Bob | approved | approved (~840 ms) |
| Unapproved model `mock/always-approve@1` | Mallory | rejected | rejected by attestor |
| Commit built on the old genesis (stale parent) | Bob | error | error, caught before sending |
| todo-js, 2 local commits → 1 PR | Carol | approved | approved |
| secure-vault, strict policy | Alice | rejected | rejected ("attestor local-dev-attestor-1 is not listed in the policy") |

**Tests:** all pass with the demo venv (Python 3.14.7, Windows 11):
- `packages/protocol`: 10 passed
- `packages/chain`: 8 passed
- `apps/client`: 20 passed. These run against the **real attestor app** in-process, so client/server wire drift breaks them.
- `services/attestor`: 15 passed (unchanged)

---

## What was added (files)

All new work is in `apps/`, `packages/`, and `demo/`. **No attestor code was touched.** The only edits to existing files are two READMEs: `apps/client/README.md` (rewritten) and `packages/protocol/README.md` (section appended).

```
apps/client/                      dosr-client (CLI + GUI)
  dosr_client/cli.py              `dosr` commands (argparse + rich)
  dosr_client/pipeline.py         Submission pipeline, step events, snapshots
  dosr_client/attestor.py         HTTP client (hand-built multipart, so upload vs. wait can be timed separately)
  dosr_client/config.py           .dosr/ files, .git/dosr/ local state, client profiles
  dosr_client/repo_ops.py         create (genesis), status, commit
  dosr_client/render.py           rich views (progress, result, latency bars)
  dosr_client/timings.py          ETA history
  dosr_client/gui/server.py       FastAPI JSON API + static SPA
  dosr_client/gui/static/         index.html, app.js, style.css (no build step; light/dark)
  tests/                          pipeline e2e + GUI API tests
packages/protocol/dosr_protocol/  canonical JSON/hashes, ULIDs, policy presets, wire builders, PR templates
packages/git-storage/dosr_git/    git wrapper + bundle creation
packages/chain/dosr_chain/        MockChain (file-backed contract stand-in)
demo/                             setup_env.py, demo.py, README, projects/, clients/
apps/.gitignore, packages/.gitignore   ignore *.egg-info from editable installs
REVIEW_NOTES_will-client-demo.md  this file
```

Repository layout the client creates:

```
<repo>/.dosr/policy.json     immutable policy; sent verbatim to the attestor; its hash is registered at genesis
<repo>/.dosr/repo.json       repo_id, chain_id, contract, policy_hash, name, creator
<repo>/.git/dosr/            local only, never committed
    submissions/<request_id>/  request.json, policy.json, change.bundle, pr.json, pr.md, response.json, run.json, chain-event.json
    timings.json               step latency history
```

---

## Compatibility with the attestor (`services/attestor`)

- The request is built by `dosr_protocol.wire.build_review_request`. It produces exactly the attestor's `ReviewRequest` schema (extra fields are forbidden). A test checks it is byte-for-byte equal to `examples/wire/1. attestor-request.json` when given the same inputs.
- Policy presets are identical to `examples/repos/*/.dosr/policy.json` (also enforced by a test). Custom policies are pre-validated client-side against the same rules.
- `ipfs_cid` is omitted. It is optional in the attestor schema, and there is no IPFS yet.
- 200 with `approved: false` is treated as a decision (rejected). 400/422 are treated as errors and the `detail` is formatted for display.
- The client checks that each response is bound to its request (`verify_response`): ids, OIDs, bundle hash, repo, model, signer listed in the policy, expiry.

### Answers to the attestor's "Open questions" (from the client side)
1. **Strict fields**: fine for the client. We send exactly the schema.
2. **200 + `approved:false`**: works well. The client exits 2 for rejected and 1 for errors.
3. **Rejected response shape**: the client handles the full attestation (what the server sends now). If fields are missing on a rejection it only warns. I'd keep the full shape and update `3. attestor-response-rejected.json` to match.
4. **`contract` vs `verifying_contract`**: the client maps them, but I'd unify on `verifying_contract` in the next protocol bump.
5. **LLM API key header**: not used yet; still undecided.

---

## ⚠️ Warnings, limitations, and things to review

1. **I could not click through the GUI in a real browser** (the Chrome extension wasn't connected in this session). The GUI is covered by API tests that follow the same flow the UI uses (create repo → edit → diff → switch profile → submit → poll → history), a JS syntax check, and live `curl` probes of the running server. Please do a manual pass before presenting.
2. **The mock chain is not a chain.** `packages/chain` is a JSON file (`demo/workspace/.chain/state.json`). It enforces the contract's deterministic checks (parent == HEAD, policy hash, attestor in policy, approved, not expired, domain). It does **not** verify signatures, because the attestor's signature is still the zero placeholder. I added it so successive PRs have a canonical parent. It also models "first registered wins" and stale-parent behaviour.
3. **The PR document is not sent to the attestor.** The attestor API has no part for it and rejects extra fields. Per the security rule, the attestor should derive the PR from the bundle itself anyway. `dosr_protocol.pr_format` is pure and dependency-free so the attestor can reuse it later and produce an identical PR (and `pr_hash`). For now the PR is a local artifact shown in the CLI and GUI.
4. **Full bundles are the default.** The attestor has no repo cache, so a thin bundle (`parent..candidate`) can't be opened server-side. Bob's profile uses `thin` to show both modes. Note that full bundles grow with history.
5. **Hashing conventions need team agreement.**
   - `policy_hash` = `0x` + sha256 of RFC 8785/JCS-canonical policy JSON (`dosr_protocol.canonical.policy_hash`).
   - The attestor's `request_hash` is sha256 of the raw request bytes, and its `response_hash` uses `json.dumps(sort_keys=True)`, not JCS.
   - `repo_id` is a prototype: sha256 of name, policy hash, creator, and a random nonce. The real contract will likely assign it, e.g. keccak.
6. **Client-side checks are conveniences only.** These checks happen in the client: policy limits, `.dosr/` immutability, stale parent, and the model warning. The attestor doesn't enforce `max_changed_files`/`max_patch_bytes`, check the bundle hash, or check that `.dosr/` is unchanged yet. These should land server-side.
7. **The model choice is not blocked client-side.** If a profile picks an unapproved model, the client warns and sends anyway. That is deliberate (the Mallory demo), because the attestor is the authority.
8. **The GUI has no authentication.** It binds to 127.0.0.1 by default and should not be exposed on a network. File paths are confined to the repo (`..` and `.git/` are rejected; there is a test for this).
9. **Upload vs. wait timing.** "Send PR" ends when the last body byte is handed to the socket. "Wait" is the rest. Locally both are tiny, so the latency you see is dominated by git subprocesses: about 100–250 ms per git-heavy step on Windows.
10. **ETA** uses defaults on the first run and then a per-repo exponential moving average.
11. **Windows notes.** The CLI forces UTF-8 stdout (cp1252 consoles mangled `…`/`→`). The GUI editor preserves CRLF if a file already uses it. `demo.py reset` handles git's read-only pack files.
12. Environment warning seen in tests: Starlette says "Using `httpx` with `starlette.testclient` is deprecated". It is harmless and also appears in the attestor's own tests.

---

## Suggested next steps

- **Attestor (Yuhan/Jakub):** verify the bundle sha256 and that the bundle contains candidate → parent. Hash the policy with `dosr_protocol.canonical.policy_hash`. Derive the diff/PR with `dosr_protocol.pr_format`. Enforce the limits and `.dosr/` immutability. Move the attestor's pydantic models into `packages/protocol` so both sides share one schema.
- **Client (Will):** IPFS publish/fetch in `packages/git-storage`. An RPC-backed adapter in `packages/chain` that replaces `MockChain` (the method names already mirror the contract). `dosr fetch`/`dosr verify`. Multi-client demo over P2P.
- **Contracts:** implement `register_repository` / `accept_transition` with the checks `MockChain` already enforces.
- Decide on one canonical serialization for `request_hash`/`response_hash`, using JCS everywhere.

---

## Repo housekeeping

- Your uncommitted local edits (`weekly-todos/10-9-2026.MD`, untracked `docs/Project Checkpoint.pdf`) were **left alone and not included** in the commit.
- `demo/.venv/` and `demo/workspace/` are git-ignored. The workspace currently holds the repos from the last scripted run, so `demo.py gui` shows them right away. Use `demo.py reset` to clear it.
