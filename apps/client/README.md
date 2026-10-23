# DOSR Client

Python CLI (`dosr`) + local web GUI for creating DOSR repositories, building PRs,
and requesting attested reviews from the attestor (`services/attestor`).

The client may be malicious or modified; no contract or attestor security decision
should depend on client-side validation alone. Client-side checks here exist only
to fail fast with good error messages.

## Install

From the repo root: `python demo/setup_env.py` (creates `demo/.venv` with the attestor
requirements and these packages installed editable). Or manually:

```bash
pip install -e packages/protocol -e packages/git-storage -e packages/chain -e "apps/client[dev]"
```

## Commands

| Command | What it does |
|---|---|
| `dosr init PATH [--seed DIR] [--preset standard\|strict\|llm-review] [--model p/m@v] [--context-file GLOB] [--max-changed-files N] …` | git init, write immutable `.dosr/policy.json` + `.dosr/repo.json`, genesis commit, register on (mock) chain |
| `dosr status` | local HEAD vs canonical HEAD, policy-hash check, uncommitted changes, attestor health |
| `dosr config [--raw]` | repository configuration / policy, with hash checks against repo.json and chain |
| `dosr diff` | committed + uncommitted changes since the canonical HEAD |
| `dosr commit -m MSG` | stage everything and commit as the current client profile |
| `dosr submit [-m MSG] [--model p/m@v] [--dry-run] [--force] [--bad-request MODE] [--no-register] [--bundle-mode full\|thin] [--show-pr] [--show-prompt] [--show-checks] [--json]` | the full PR pipeline with live progress. Local validation **blocks** a request the attestor would reject (exit 3); `--force` sends it anyway (simulates a modified client). `--bad-request` (demo) tampers with one part: `unapproved-model`, `outdated-model`, `stale-parent`, `parent-equals-candidate`, `wrong-object-format`, `bundle-hash-mismatch`, `tampered-prompt`, `wrong-prompt-template`, `modified-policy` (→ blocked, or forced → signed REJECTED), `malformed-request` (forced → 422), `empty-bundle` (forced → 400) |
| `dosr history` / `dosr show [ID] [--pr] [--prompt] [--response]` | past submissions and their artifacts |
| `dosr server` | attestor health + keys |
| `dosr chain` / `dosr register` | inspect mock chain / register an existing repo's genesis |
| `dosr profiles --dir DIR` | list client profiles |
| `dosr publish [--announce PEER_URL]` | publish the canonical HEAD (full git bundle) + the signed attestation of every accepted transition to IPFS; prints the **share code** (manifest CID) |
| `dosr import SHARE [--workspace DIR] [--expect-head OID] [--adopt]` / `dosr pull SHARE` | clone a shared repo, or update your copy: verifies the bundle digest, genesis + policy, and replays every signed transition on your (mock) chain |
| `dosr p2p [--connect MULTIADDR]` | IPFS node status: peer id, addresses, connected peers |
| `dosr gui [--workspace DIR] [--seeds DIR] [--profiles DIR] [--p2p-port 8790] [--device NAME]` | local web GUI on http://127.0.0.1:8765 (with `--p2p-port`: plus the LAN peer inbox) |

Global options go before the subcommand: `-C/--repo`, `--profile`, `--attestor-url`,
`--chain-state`, `--ipfs-api`, `--local-store`, `--p2p auto|ipfs|local|off` (env: `DOSR_PROFILE`,
`DOSR_ATTESTOR_URL`, `DOSR_CHAIN_STATE`, `DOSR_IPFS_API`, `DOSR_LOCAL_STORE`).

Exit codes for `submit`: 0 approved / dry-run, 2 rejected (by the attestor), 3 blocked (by local validation), 1 error.

Model versions in a policy may be ranges (`--model mock/reviewer@^1.2.0`, or for dated snapshots
`--model openai/gpt-4o@>=2024-08-06`); a submission asks for one concrete version
(`--model mock/reviewer@1.4.2`), by default the lowest the range allows.

**Common models.** `dosr models` lists OpenAI, Anthropic (Claude), Google Gemini, Zhipu and mock
models with their versions, the exact API model id, whether your key is set, and (inside a repo)
whether the policy approves them. `--model` accepts `provider/model` without a version (taken
from the policy, else the newest known snapshot) and the aliases `claude` → `anthropic`,
`google` → `gemini`, e.g. `dosr submit --model claude/claude-sonnet-4-5`. Any other
`provider/model@version` can still be typed by hand. Keys come from `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `ZHIPU_API_KEY` (environment or the
repo's `.env`). In the GUI:
- **🔑 API keys** (top right) sets a key without restarting. It updates the environment the
  client reads at submit time and, with `dosr gui --env-file PATH` (the demo passes the repo's
  `.env`), saves it there. Keys are never sent back to the browser: only `••••••••` plus the last
  4 characters (`keys.py`, `/api/keys`).
- **New repo:** one row of provider → model → version dropdowns per approved model, with
  "or newer" and *Custom…* (free text) rows.
- **Submit:** the model menu shows only approved models, graying out those without a key.
  *Force send* also offers all other common models plus a free-text model box.
`--context-file` adds files every review prompt must include (stored as `review.context_files`).

**PR templates** are TOML files: what is collected from Git, the reviewer instructions, and the PR
layout (format: `demo/templates/README.md`, code: `dosr_protocol/templates.py`). `dosr template
list | show [ID|FILE] [--toml] [--preview] | new ID [--from ID|FILE] | check FILE [--preview]`;
`dosr init --template ID|FILE` embeds a custom template in the policy (`review.template`). Ids
are looked up in `$DOSR_TEMPLATES` and `~/.dosr/templates`. The GUI's new-repo dialog has a
template viewer/editor with a live preview.

## Submission pipeline (`dosr_client/pipeline.py`)

| Step | Notes |
|---|---|
| Preflight checks | policy valid + hash matches repo.json and chain; template known; model choice; attestor health/keys |
| Build git commit | commits uncommitted changes with `-m` (as the profile's identity), else uses HEAD |
| Resolve canonical parent | parent = chain HEAD; candidate must descend from it (stale parent → error) |
| Bundle changes | `git bundle` (`full` = self-contained, default; `thin` = parent..candidate); sha256 |
| Build PR | per the policy's `prompt_template` plus `review.context_files`: diff context, full files, context files |
| Build review prompt | `decision.build_prompt(template, pr markdown, decision_schema)`: exactly what the model will see; saved as `prompt.txt` |
| Build review request | wire JSON matching `examples/wire/1. attestor-request.json` (`--bad-request` tampers here) |
| Validate locally | `validate.py`: the attestor's own checks, same names, on the exact bytes about to be sent. Re-opens the bundle in a scratch repo and rebuilds the PR and prompt from it (`pr_matches_bundle`, `client_prompt_matches`), checks the chain (policy at genesis, parent = HEAD), `/v1/keys` against the policy, model + version range, limits, `.dosr/` immutability, provider key. Any failure → **blocked** unless `--force` |
| Send PR to attestor | multipart `request` + `policy` + `bundle` + `prompt`; timed until the last byte is sent |
| Wait for attestor decision | time from upload complete to response; records whether the model or a deterministic check decided |
| Verify attestation | EIP-712 signature recovers to a policy-listed attestor; bound to our request; signed `review_input_hash` = our prompt hash; attestor's derived `pr_hash` = ours; client vs attestor check disagreements are flagged |
| Register on chain (mock) | approved only; parent must still equal HEAD |
| Publish to IPFS | after a registered approval: `p2p.publish` (share code); the GUI also announces it to its peers. Skipped with `--no-publish` / `--p2p off` |

Each submission also records **PR size** (`pr-metrics.json`: words/chars/lines/bytes and a rough
chars/4 token estimate, for the whole rendered PR and per section: diff, full files, context files,
description, instructions) and **transfer sizes** (request/policy/bundle bytes sent, total upload incl.
multipart framing, response bytes received). Both appear in the CLI result, `dosr history`, and the GUI.

Each step records status, latency, detail, and error; remaining-time estimates come
from a per-repo moving average in `.git/dosr/timings.json`. Artifacts for every
submission (request, policy, bundle, PR json/markdown, response, run summary) are
kept in `.git/dosr/submissions/<request_id>/` — never committed. That includes `prompt.txt` (sent) and
`prompt.derived.txt` (rebuilt from the bundle during validation; identical for an honest client).

## P2P sharing (`p2p.py`, `ipfs.py`, `inbox.py`)

- **Storage** (`ipfs.py`): a minimal Kubo RPC client (`add`, `cat`, `pin`, `swarm/connect`,
  `id`) at `$DOSR_IPFS_API` (default http://127.0.0.1:5001). In `auto` mode, if no node answers,
  a sha256-addressed local directory (`local:<hex>` ids) is used instead, so everything still
  works on one machine.
- **Share manifest** (`dosr-share` v0.1): repo metadata, policy hash, genesis, canonical HEAD,
  the bundle's id + sha256, and every accepted transition's `{attestation, signature}` in order.
  The client never trusts it: on import it checks the bundle digest, that the bundle head is the
  claimed HEAD, that the root commit is the genesis with the claimed policy (and `.dosr/`
  unchanged), and that the transitions chain from genesis to HEAD. It then replays them through
  `MockChain.accept_transition` (EIP-712 signer in the genesis policy, approved, parent == HEAD)
  on a scratch copy of the chain first, so a half-valid share never changes anything.
- **Chain stand-in** (dev): the first import registers the genesis on the importer's mock chain
  (trust on first use, standing in for "read the contract"). A fork (both devices advanced the same
  HEAD) is a conflict; `--adopt` / *Adopt their history* drops the local history for that repo.
- **Peer inbox** (`inbox.py`, `dosr gui --p2p-port`): a separate, unauthenticated LAN endpoint
  (`GET/POST /dosr/hello`, `POST /dosr/announce`) that only stores share-code hints. The GUI API
  itself stays on 127.0.0.1. Adding a peer is mutual, and it also dials the peer's IPFS node.

## Tests

```bash
cd apps/client && pytest     # runs against the real attestor app in-process
```
