# DOSR demo (week of 10/23)

Each computer runs the attestor (`services/attestor`), the client application (CLI + GUI), an
IPFS node, and a **mock chain**: a local JSON file (`demo/workspace/.chain/state.json`) that
stands in for the contract and holds each repository's canonical HEAD and genesis policy hash.
Git bundles, PR building, prompts, local validation, attestor verification, EIP-712 signatures,
LLM review (OpenAI, Claude, Gemini or Zhipu GLM), and **P2P sharing between computers over
IPFS** ([section 3](#3-p2p-share-repositories-between-two-computers-ipfs)) are real. Only the
real chain is missing; until it exists, share codes are passed between devices by hand or
through a dev peer inbox.

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
| **P2P over IPFS**: publish the canonical HEAD + signed history; another computer imports it, re-verifies every signature, lands its own PRs, and shares them back | *Publish to IPFS* step, **Share (P2P)** tab, **P2P** pill (peers, inbox, import); `dosr publish` / `import` / `pull` |

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

Double-click one of these: `.bat` on Windows, `.command` on macOS. The first time on macOS, use
right-click → Open; if a file isn't executable, run `chmod +x demo/*.command`.

| Launcher | What it runs |
|---|---|
| `demo/start-demo` | **Local demo**: one client on this computer, no IPFS. |
| `demo/start-ipfs-demo` | **IPFS demo across two computers**: run it on each. It adds an IPFS node and the peer inbox ([section 3](#3-p2p-share-repositories-between-two-computers-ipfs)). |
| `demo/start-two-clients` | **IPFS demo on one computer**: two clients (*Laptop A* = `demo/workspace`, *Laptop B* = `demo/workspace-b`), each with its own attestor and IPFS node, already peered, one browser tab each. |

Each launcher creates `demo/.venv` on first run. The IPFS launchers also download IPFS (Kubo,
about 40 MB, checked against its published sha512) into `demo/.ipfs/` the first time.

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
PY demo/demo.py up          # IPFS + attestor on :8080 + GUI on :8765, opens the browser
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

## 3. P2P: share repositories between two computers (IPFS)

Each computer runs its own DOSR: client, attestor, mock chain, and an **IPFS node**. Publishing
puts a git bundle of the canonical HEAD on IPFS, plus a small manifest holding the signed
attestation for every accepted PR. The manifest's CID is the **share code**. The other computer
imports the share code, and its client checks everything before its copy moves: the bundle
sha256, that the history starts at the genesis with the registered policy, and every EIP-712
signature (replayed on its own mock chain, which also enforces parent == HEAD).

**No blockchain yet.** With a real contract, both devices would read the canonical HEAD from
the chain. For now, the share code carries the signed history, and you pass it between devices
in one of two ways:
- **Automatically:** add each other as *peers* once. Every publish then lands in the other
  device's **inbox** (a small dev-only HTTP endpoint on port 8790).
- **By hand:** copy the share code (and, optionally, the commit hash) into chat, then paste it
  into *Import* on the other device.

### Setup (each computer, once)

1. Pull the latest code. Nothing else: the launcher sets up the venv and downloads IPFS on first
   run. (If you set up the venv before, re-running `python demo/setup_env.py` is harmless.)
2. Put both computers on the **same Wi-Fi/LAN**. Guest or campus Wi-Fi that isolates clients
   blocks this; a phone hotspot works.

### Run (each computer)

Double-click **`demo/start-ipfs-demo`** (`.bat` / `.command`), or run `PY demo/demo.py up`.

This starts IPFS, the attestor, the GUI, and the peer inbox. The first time, **Windows Firewall
asks about `ipfs.exe` and Python: click Allow** (private networks). On macOS, allow incoming
connections if asked. The top-right pill should turn green: **IPFS · N peers**.

### Connect the two computers (once)

1. On computer A, click the **P2P** pill (top right). Under *This device*, *Your address* shows
   something like `http://192.168.1.20:8790`. Send that to B.
2. On computer B: **P2P → Peers →** paste it **→ Add peer**. Peering is mutual: A now lists B
   too, and B's IPFS node dials A's directly.

### Share a repository

| Step | Computer A | Computer B |
|---|---|---|
| 1 | **+ New** → create e.g. `calculator-py`. It is published automatically; the **Share (P2P)** tab shows the share code. | The share code appears in the inbox (badge on the P2P pill). |
| 2 | | **P2P → Inbox → Import** (or sidebar **Import** → paste the share code → *Import / pull*). The repo is cloned, and its genesis is registered on B's mock chain. |
| 3 | | Edit a file → **Submit PR**. B's own attestor reviews it. On approval it is published and announced back to A (*Publish to IPFS* step). |
| 4 | **P2P → Inbox → Pull**. B's signed transition is verified and applied, and the files fast-forward. | |
| 5 | Keep going, in either direction. | |

**Take turns.** If both computers land a PR on the same parent before pulling, the second pull
reports a **conflict** (two transactions racing on a real chain would do the same thing). The
error offers **Adopt their history (dev)**, which drops your conflicting change for that repo
(it stays in `git reflog`).

### Without a second computer

- **Two clients on one computer, over real IPFS:** double-click **`demo/start-two-clients`**
  (or run `PY demo/demo.py pair`). Laptop A (Alice, http://127.0.0.1:8765) and Laptop B (Bob,
  http://127.0.0.1:8766) each get their own workspace, mock chain, attestor, and IPFS node, so
  data really moves between two nodes. They come up already peered, so follow the table above
  starting at step 1. Nothing listens on the network in this mode (no firewall prompts). Use
  `PY demo/demo.py --device b reset` to wipe Laptop B.
- **Local demo, no sharing:** `demo/start-demo` works exactly as before. Publishing falls back to
  a local store (`demo/.ipfs/local-store`, share codes `local:…`); the pill reads *local store
  (no IPFS)*.
- **Scripted:** scenario 10 of `PY demo/demo.py run` does the round trip (A publishes, device B
  imports, B lands a PR, A pulls it), over IPFS if the node is running.

### CLI equivalents

```bash
PY -m dosr_client.cli -C demo/workspace/calculator-py publish          # prints the share code
PY -m dosr_client.cli import bafy… --workspace demo/workspace           # clone (or update)
PY -m dosr_client.cli -C demo/workspace/calculator-py pull bafy…        # update an existing copy
PY -m dosr_client.cli p2p                                               # IPFS peer id, addresses, peers
```

(Add `--chain-state demo/workspace/.chain/state.json` so the CLI uses the demo's chain, as the GUI does.)

### Troubleshooting

| Symptom | Fix |
|---|---|
| Pill says *local store (no IPFS)* | `PY demo/demo.py ipfs install`, then restart `demo.py up`. IPFS Desktop also works (it uses the same API port, 5001). |
| *Add peer* fails | Check that both are on the same network, that the address is the one under *This device*, and that the firewall allows Python on 8790. Or skip peers and paste share codes by hand. |
| Import says *could not find … on IPFS* | The publishing computer must be online with `demo.py up` running. *Add peer* makes the two IPFS nodes connect directly; or use *P2P → Dial a node* with an `/ip4/…/tcp/4001/p2p/…` address from the other device. Allow port 4001 in the firewall. |
| *conflict* | See *Take turns* above. |
| Port in use | Defaults: attestor 8080, GUI 8765, inbox 8790, IPFS API 5001 / swarm 4001 / gateway 8089. Laptop B in `start-two-clients`: each port +1. |

Code: `apps/client/dosr_client/p2p.py` (publish/import/verify), `ipfs.py` (Kubo RPC client and
local fallback), `inbox.py` (peer inbox). Tests: `apps/client/tests/test_p2p.py`.

## Contents

- `projects/`: seed projects. `files/` is the initial tree, `changes/<id>/` is overlaid for
  scripted PRs, `seed.json` has description, preset, `policy_overrides` (approved models with
  version ranges, extra context files) and expected outcomes.
- `clients/`: fake client profiles (git identity, review-model choice, bundle mode).
- `templates/`: custom PR template files (see [templates/README.md](templates/README.md) for the
  format). `demo.py` adds this folder to `DOSR_TEMPLATES`, and GUI-saved templates land here.
- `workspace/`: generated repos + `.chain/state.json` (git-ignored). `workspace-b/` etc.: the
  same for simulated extra devices (`--device b`).
- `.ipfs/`: the IPFS binary (`bin/`), the node's data (`repo/`), and the local fallback store
  (git-ignored).
