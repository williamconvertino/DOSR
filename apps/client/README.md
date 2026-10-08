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
| `dosr init PATH [--seed DIR] [--preset standard\|strict] [--model p/m@v] [--max-changed-files N] …` | git init, write immutable `.dosr/policy.json` + `.dosr/repo.json`, genesis commit, register on (mock) chain |
| `dosr status` | local HEAD vs canonical HEAD, policy-hash check, uncommitted changes, attestor health |
| `dosr config [--raw]` | repository configuration / policy, with hash checks against repo.json and chain |
| `dosr diff` | committed + uncommitted changes since the canonical HEAD |
| `dosr commit -m MSG` | stage everything and commit as the current client profile |
| `dosr submit [-m MSG] [--model p/m@v] [--dry-run] [--no-register] [--bundle-mode full\|thin] [--show-pr] [--json]` | the full PR pipeline with live progress |
| `dosr history` / `dosr show [ID] [--pr] [--response]` | past submissions and their artifacts |
| `dosr server` | attestor health + keys |
| `dosr chain` / `dosr register` | inspect mock chain / register an existing repo's genesis |
| `dosr profiles --dir DIR` | list client profiles |
| `dosr gui [--workspace DIR] [--seeds DIR] [--profiles DIR]` | local web GUI on http://127.0.0.1:8765 |

Global options go before the subcommand: `-C/--repo`, `--profile`, `--attestor-url`,
`--chain-state` (env: `DOSR_PROFILE`, `DOSR_ATTESTOR_URL`, `DOSR_CHAIN_STATE`).

Exit codes for `submit`: 0 approved / dry-run, 2 rejected, 1 error.

## Submission pipeline (`dosr_client/pipeline.py`)

| Step | Notes |
|---|---|
| Preflight checks | policy valid + hash matches repo.json and chain; template known; model choice; attestor health/keys |
| Build git commit | commits uncommitted changes with `-m` (as the profile's identity), else uses HEAD |
| Resolve canonical parent | parent = chain HEAD; candidate must descend from it (stale parent → error) |
| Bundle changes | `git bundle` (`full` = self-contained, default; `thin` = parent..candidate); sha256 |
| Build PR | per the policy's `prompt_template`: diff context, full files, context files; enforces `max_changed_files`/`max_patch_bytes`; refuses edits to `.dosr/` |
| Build review request | wire JSON matching `examples/wire/1. attestor-request.json` |
| Send PR to attestor | multipart `request` + `policy` + `bundle`; timed until the last byte is sent |
| Wait for attestor decision | time from upload complete to response |
| Verify attestation | response bound to our request (oids, bundle hash, repo, model, signer in policy, expiry) |
| Register on chain (mock) | approved only; parent must still equal HEAD |

Each step records status, latency, detail, and error; remaining-time estimates come
from a per-repo moving average in `.git/dosr/timings.json`. Artifacts for every
submission (request, policy, bundle, PR json/markdown, response, run summary) are
kept in `.git/dosr/submissions/<request_id>/` — never committed.

## Tests

```bash
cd apps/client && pytest     # runs against the real attestor app in-process
```
