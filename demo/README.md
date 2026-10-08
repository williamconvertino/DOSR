# DOSR demo (week of 10/9)

Everything runs on one machine: the attestor (`services/attestor`), one client
application (CLI + GUI), and a file-backed **mock** chain. No IPFS, real chain, LLM,
or real signatures yet.

## One-click start

- **Windows:** double-click `demo\start-demo.bat`
- **macOS:** double-click `demo/start-demo.command` (first time: right-click → Open if Gatekeeper complains;
  if it isn't executable, run `chmod +x demo/start-demo.command`)

On first run these create `demo/.venv` automatically, then start the attestor + GUI and open
the browser. Close the window to stop. The manual steps below do the same thing.

## 1. Setup (once)

Requires Python 3.10+ and git.

```bash
python demo/setup_env.py
```

This creates `demo/.venv` and installs the attestor requirements plus the DOSR
packages. Below, `PY` means the venv's Python:

- Windows: `demo\.venv\Scripts\python.exe`
- macOS/Linux: `demo/.venv/bin/python`

## 2a. Live GUI demo

```bash
PY demo/demo.py up          # starts attestor on :8080 + GUI on :8765, opens browser
```

or in two terminals: `PY demo/demo.py attestor` and `PY demo/demo.py gui`.

Suggested walkthrough:

1. **Profile switcher** (top right): pick *Alice*. The pill next to it shows attestor health/latency.
2. **+ New** → seed *calculator-py* (fills name/description/preset) → *Create repository*.
   This runs git init, writes the immutable `.dosr/policy.json`, makes the genesis commit,
   and registers it on the mock chain.
3. **Files** tab: open `calculator/ops.py`, add a function, *Save* (Ctrl+S).
   (`.dosr/policy.json` is read-only — the policy is immutable.)
4. **Changes** tab: diff vs the canonical HEAD.
5. Switch profile to *Bob*. **Submit PR** tab: enter a commit message, *Submit to DOSR*.
   Watch each step live (status, per-step latency, elapsed / remaining estimate), then the
   decision card with latency breakdown, warnings, and errors.
6. Switch to *Mallory* (requests an unapproved model), make another edit, submit → **REJECTED**.
   Use *Discard uncommitted changes* / submit something else to continue.
7. **Bad request demo:** make an edit, pick a mode under *Demo: send a bad request*, and submit.
   The first three modes get a signed **REJECTED** review back from the attestor (its real reject
   messages); the last two show how attestor validation errors are displayed:

   | Mode | What's wrong | Attestor answer |
   |---|---|---|
   | `unapproved-model` | asks for `mock/always-approve@1` | rejected: model not approved |
   | `parent-equals-candidate` | candidate OID = parent OID | rejected: candidate is the same commit as parent |
   | `wrong-object-format` | sha256 OIDs in a sha1 repo | rejected: OIDs don't use the policy's object format |
   | `malformed-request` | adds an unknown JSON field | HTTP 422 (schema) |
   | `empty-bundle` | sends a 0-byte bundle | HTTP 400 |

   CLI equivalent: `dosr submit -m "msg" --bad-request parent-equals-candidate`.
   Every result shows **PR size** (words, chars, lines, ~tokens) and **Transfer** (bytes sent per
   part / bytes received); the *PR size by section* card breaks the word count down further.
8. **History** tab: every submission, with the generated PR document, request JSON, and
   attestor response. **Config** tab: policy, hashes, and the mock-chain record.
9. Create *secure-vault* (strict preset) and submit anything → rejected because the strict
   policy does not trust the local attestor.

## 2b. Scripted CLI demo

```bash
PY demo/demo.py run --reset --start-attestor          # add --pause to step through
```

| # | Client | Scenario | Expected |
|---|---|---|---|
| 1 | Alice | create `calculator-py` from seed | created |
| 2 | Bob | add `power()`/`modulo()` (thin bundle) | **approved**, canonical HEAD advances |
| 3 | Mallory | request review from `mock/always-approve@1` | **rejected** by attestor |
| 4 | Bob | commit on top of the old genesis commit | **error**: stale parent, caught before sending |
| 5 | Carol | create `todo-js`, 2 local commits, one PR | **approved** |
| 6 | Alice | `secure-vault` (strict policy) change | **rejected**: attestor not in policy |
| 7 | Bob | `--bad-request parent-equals-candidate` | **rejected** by attestor |

Afterwards `PY demo/demo.py gui` shows the same repos and history in the GUI.
`PY demo/demo.py reset` wipes `demo/workspace/` (repos + chain state).

## Contents

- `projects/` — seed projects: `files/` is the initial tree, `changes/<id>/` is overlaid
  for scripted PRs, `seed.json` has description/preset/expected outcomes.
- `clients/` — fake client profiles (git identity, review-model choice, bundle mode).
- `workspace/` — generated repos + `.chain/state.json` (git-ignored).
