# DOSR demo (week of 10/16)

Everything runs on one machine: the attestor (`services/attestor`), the client application
(CLI + GUI), and a **mock chain**, a local JSON file (`demo/workspace/.chain/state.json`)
that stands in for the contract and holds each repository's canonical HEAD and genesis policy
hash. Git bundles, PR building, prompts, local validation, attestor verification, EIP-712
signatures, and LLM review (OpenAI, Claude, Gemini or Zhipu GLM) are real. Only P2P replication (IPFS) and a real chain
are missing.

What the demo shows:

| Part of DOSR | Where you see it |
|---|---|
| Create a repo with an immutable policy (models + **minimum versions**, PR template, **extra context files**, attestors, limits) | GUI *+ New* / `dosr init`; Config tab |
| **Custom PR templates** (TOML files: what is collected, reviewer instructions, PR layout), embedded in the policy | *+ New* → PR template → *View* / *+ New template…*; `dosr template`; todo-js uses `templates/team-review-v1.toml` |
| Edit, commit, and propose a change | Files / Changes / Submit PR tabs; `dosr submit` |
| PR + **exact review prompt** built per the policy, sent to the attestor | *Build PR*, *Build review prompt* steps; Artifacts → *Prompt sent to attestor* |
| **Client-side validation**: the attestor's checks, run locally on the exact bytes (bundle re-opened, PR + prompt rebuilt) | *Validate locally* step; **Checks** card (client vs attestor, side by side) |
| Mismatch (head, checksum, prompt template, model version, ...) → **client stops it** | outcome **BLOCKED** (nothing sent) |
| User forces it through (modified client) → **attestor rejects** | *Force send* / `--force` → signed **REJECTED** |
| Well-formed PR → attestor verifies, **LLM reviews**, attestor **signs** | outcome **APPROVED**; *Signed attestation* card |
| Client verifies the attestation, registers it on the (mock) chain | *Verify attestation* + *Register on chain* steps; canonical HEAD moves |
| Latency of every step, sizes, tokens | progress list, *Latency by step*, *PR size by section*, Transfer row |

## LLM key (for real LLM review: OpenAI, Claude, Gemini or GLM)

The contributor pays for review with their own key. Easiest: in the GUI, click **🔑 API keys**
(top right), paste the key and *Save*. It is used immediately (no restart) and saved to the
repo's `.env` (git-ignored). The dialog only ever shows the last 4 characters, so it is safe to
present. Or edit `.env` by hand before starting:

```bash
echo "ANTHROPIC_API_KEY=<key>" >> .env     # or OPENAI_API_KEY / GEMINI_API_KEY / ZHIPU_API_KEY
```

The scripted demo then creates `calculator-py` on the `llm-review` policy approving the model of
the first provider with a key: `anthropic/claude-sonnet-4-5@20250929`,
`openai/gpt-4.1@2025-04-14`, `gemini/gemini-2.5-flash@stable` or `zhipu/glm-4-plus@api-v4`
(override with `DEMO_LLM_MODEL=provider/model@version`). `demo.py` loads `.env` for the
attestor and the clients. The client sends the key with each review request
(`X-DOSR-Provider-Key`); the attestor uses it for that one model call and never stores it.
**Without a key**, the scripted demo creates calculator-py on the `standard` (mock reviewer)
policy and skips scenario 9. The Submit tab shows whether the selected model's key is set.

**Picking models.** `PY -m dosr_client.cli models` lists the common OpenAI, Claude, Gemini and
GLM models and versions. In the GUI's *+ New* dialog, each approved model is one row of
provider → model → version dropdowns (tick *or newer* for a minimum version such as
`>=2024-08-06`); *+ Add another model* adds a row, ✕ removes one. Choose **Custom…** as the
provider to type any `provider/model@version` (other providers, range syntax). On the Submit tab
the model menu lists only the policy's approved models; ones whose API key is missing are grayed
out (still selectable: the client then blocks with `provider_key_available`). Ticking **Force
send** adds every other common model and a free-text box, to show the attestor rejecting an
unapproved model.

## One-click start

- **Windows:** double-click `demo\start-demo.bat`
- **macOS:** double-click `demo/start-demo.command` (first time: right-click → Open; if it isn't
  executable, `chmod +x demo/start-demo.command`)

These create `demo/.venv` on first run, then start the attestor + GUI and open the browser.

## 1. Setup (once)

Requires Python 3.10+ and git. Re-run after pulling (new dependencies, e.g. `eth-account`):

```bash
python demo/setup_env.py
```

Below, `PY` means the venv's Python: `demo\.venv\Scripts\python.exe` (Windows) or
`demo/.venv/bin/python` (macOS/Linux).

## 2a. Scripted CLI demo (best first run)

```bash
PY demo/demo.py run --reset --start-attestor          # add --pause to step through when presenting
```

| # | Client | Scenario | Expected |
|---|---|---|---|
| 1 | Alice | create `calculator-py` (policy adds `docs/*.md` as review context) | created |
| 2 | Bob | add `power()`/`modulo()`; full checks table printed | **approved**, canonical HEAD advances |
| 3 | Mallory | asks for `mock/always-approve@1` | **blocked** by her client (`model_is_approved`) |
| 4 | Mallory | same, `--force` | **rejected** by the attestor (signed) |
| 5 | Bob | work branched from the old genesis commit | **error**: stale parent, caught before anything is built |
| 6 | Bob | `--bad-request` × 5: `stale-parent` (head), `bundle-hash-mismatch` (checksum), `wrong-prompt-template`, `tampered-prompt`, `modified-policy`, each without and with `--force` | **blocked**, then **rejected** |
| 7 | Carol | create `todo-js` (policy: `mock/deterministic-reviewer@^1.2.0`, custom PR template `team-review-v1`); 2 commits → 1 PR; model 1.1.0 → blocked, forced → rejected; model 1.4.2 | **blocked**, **rejected**, **approved** |
| 8 | Alice | `secure-vault` (strict policy, does not trust this attestor) | **blocked**, forced → **rejected** |
| 9 | Mallory | `evaluate()` with `eval()` + "pre-approved" comment, reviewed by the real LLM (needs a key) | **rejected by the LLM**, issues listed |

Everything the attestor rejects in 3–8 is stopped by deterministic checks *before* any model
call (so it costs nothing). Scenario 9 passes every check and is rejected by the model itself.

## 2b. Live GUI demo

```bash
PY demo/demo.py up          # attestor on :8080 + GUI on :8765, opens the browser
```

(or `PY demo/demo.py attestor` and `PY demo/demo.py gui` in two terminals). After a scripted run
the GUI shows the same repos and history. Suggested walkthrough:

1. **Profile switcher** (top right): pick *Alice*. The pill shows attestor health and latency.
   **🔑 API keys** next to it: add a provider key (shown masked, e.g. `••••••••wxyz`).
2. **+ New** → seed *todo-js* (after `demo.py reset`, or give it another name, since the
   scripted run already created one). The dialog fills in the seed's policy: approved model
   `mock/deterministic-reviewer@^1.2.0` (a minimum version) and extra context files
   `docs/*.md, package.json`, and the custom PR template `team-review-v1` (*View* shows the file
   and a preview; *+ New template…* opens the editor to make your own: copy any template, edit
   the TOML, *Check & preview*, *Save*). *Create repository* runs git init, writes the immutable
   `.dosr/policy.json`, makes the genesis commit, and registers it on the mock chain.
   **Config** tab: the policy, hashes, and the mock-chain record.
3. **Files**: edit `src/todo.js`, *Save* (Ctrl+S). `.dosr/policy.json` is read-only.
   **Changes**: diff against the canonical HEAD.
4. Switch to *Bob*. **Submit PR**: commit message → *Submit to DOSR*. Watch the 12 steps live
   (status, latency, remaining time). Then:
   - **Checks** card: every attestor check, run by the client before sending and again by the
     attestor, side by side.
   - **Signed attestation**: who decided (model vs. deterministic check), signer, request/prompt/
     model-output hashes, signature.
   - **Artifacts** → *Prompt sent to attestor*: the exact prompt, laid out by `team-review-v1`
     (rules first, then the patch, then `docs/STYLE.md`).
5. **Attack demos** box: make another edit, pick e.g. *tampered-prompt* or
   *bundle-hash-mismatch*, submit → **Blocked by your client** (the failing check is listed;
   nothing was sent). Pick it again, tick **Force send**, submit → **Rejected by the attestor**,
   with the same check failing in the attestor column.
6. Model version: the menu offers the versions `^1.2.0` allows (`1.4.2`, `1.2.0`); pick `1.4.2`
   → approved. For a too-old version, use the attack demo *outdated-model* (→ blocked), or tick
   **Force send** and type `mock/deterministic-reviewer@1.1.0` in the box → rejected by the
   attestor.
7. Switch to *Mallory* (asks for an unapproved model) → blocked; with Force send → rejected.
8. **History**: every submission, with its PR document, prompt, request, and attestor response.

| Bad-request mode | What it breaks | Client check that blocks it |
|---|---|---|
| `unapproved-model` / `outdated-model` | model not in the policy / below its minimum version | `model_is_approved` |
| `stale-parent` | claims an older parent than canonical HEAD | `parent_is_canonical_head` |
| `parent-equals-candidate`, `wrong-object-format` | malformed transition | `candidate_differs_from_parent`, `*_git_oid_format` |
| `bundle-hash-mismatch` | bundle bytes ≠ checksum in the request | `bundle_hash_matches` |
| `tampered-prompt` / `wrong-prompt-template` | prompt ≠ what the policy + bundle produce | `client_prompt_matches` |
| `modified-policy` | loosened policy with a recomputed hash | `policy_hash_matches_genesis` |
| `malformed-request` / `empty-bundle` | schema / empty part (forced → HTTP 422 / 400) | `request_is_well_formed` / `bundle_hash_matches` |

`PY demo/demo.py reset` wipes `demo/workspace/` (repos + chain state + attestor cache).

## Contents

- `projects/`: seed projects. `files/` is the initial tree, `changes/<id>/` is overlaid for
  scripted PRs, `seed.json` has description, preset, `policy_overrides` (approved models with
  version ranges, extra context files) and expected outcomes.
- `clients/`: fake client profiles (git identity, review-model choice, bundle mode).
- `templates/`: custom PR template files (see [templates/README.md](templates/README.md) for the
  format). `demo.py` adds this folder to `DOSR_TEMPLATES`, and GUI-saved templates land here.
- `workspace/`: generated repos + `.chain/state.json` (git-ignored).
