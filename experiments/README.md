# DOSR review evaluation harness (`dosr_eval`)

Measures how well a review **template × model** pair judges PRs, and what it costs.
It runs labeled cases through the **same code path the attestor uses** to build the
prompt and call the model, so the results describe DOSR as deployed:

```
case ─► git parent/candidate ─► dosr_git.pr_inputs ─► pr_format.build_pr ─► render_markdown
     ─► decision.build_prompt ─► attestor.reviewers (same request body) ─► decision.parse_decision
```

`tests/test_fidelity.py` checks that the prompt hash here equals the one the attestor
computes from a Git bundle of the same transition. Unparseable model output counts as
a rejection, exactly as in the attestor.

Owners: Jakub + Gage (prompt-format and model evaluation, checkpoint §5).

## Setup

From the repo root, with the demo venv (`python demo/setup_env.py` first):

```bash
demo/.venv/bin/python -m pip install -e "experiments[dev,figures]"   # figures = matplotlib, optional
cd experiments
../demo/.venv/bin/python -m pytest      # offline, no API key needed
```

**Nothing costs money by default.** The default model is the mock reviewer, and any
other model (`openai/…`, `anthropic/…`, `gemini/…`, `zhipu/…`) only prints the plan
and cost estimate unless you pass `--paid`. `--budget` then stops the run before the
next call once that much is spent.

Real models go through the attestor's own backends (same request body as
production) and use the contributor's key, as in DOSR: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `ZHIPU_API_KEY` in the repo's `.env`
(git-ignored) or the environment. `dosr_eval models` shows which are set. Other
OpenAI-compatible providers: pass `--providers-file` (attestor format) and set
`<PROVIDER>_API_KEY`.

## Commands

Run from `experiments/` (`PY` = the venv's Python):

```bash
PY -m dosr_eval list                                   # cases and their labels
PY -m dosr_eval --cases cases/pilot-v1 list            # one suite only
PY -m dosr_eval prompt demo-calc-evaluate-injection    # exact prompt sent to the model
PY -m dosr_eval models                                 # providers and whether their key is set

# The pilot, free: mock reviewer (plumbing check)
PY -m dosr_eval --cases cases/pilot-v1 run \
   --template security-sensitive-review-v2 --template exp-evidence-v1 --label mock

# The pilot with real models: prints the plan and stops unless --paid is given
PY -m dosr_eval --cases cases/pilot-v1 run \
   --template security-sensitive-review-v2 --template exp-evidence-v1 \
   --model openai/gpt-5@2025-08-07 --model anthropic/<model>@latest \
   --pricing pricing.json --budget 5 --paid --label pilot

PY -m dosr_eval report results/<run>.jsonl             # re-score a finished run
PY -m dosr_eval grade results/<run>.jsonl              # CSV of correct rejections to grade (y/n)
PY -m dosr_eval report results/<run>.jsonl --grades results/<run>.grades.csv
PY -m dosr_eval figures results/<run>.jsonl --grades results/<run>.grades.csv --currency USD

# Several runs at once (e.g. a rerun of replaced cases), dropping a mislabelled case
PY -m dosr_eval figures results/<run1>.jsonl results/<run2>.jsonl \
   --grades results/<run1>.grades.csv --grades results/<run2>.grades.csv --exclude <case-id>
```

`figures` writes `reports/<run>/tables.tex` (booktabs; `\input{}` it) and
`fig_verdicts.pdf` / `fig_misleading.pdf` (+ PNG), sized for the USENIX column.

Model specs are `provider/model@version`. A dated version is appended to the model id
(`openai/gpt-5@2025-08-07` sends `gpt-5-2025-08-07`); any other version, e.g.
`@latest`, sends the model name as is.

Defaults: template `security-sensitive-review-v2`, model `mock/deterministic-reviewer-v1@1`
(approves everything; for plumbing checks only).

Each run writes `results/<run_id>.jsonl` (one row per model call) and
`results/<run_id>.meta.json` (cases, templates, models, reps, DOSR commit). `results/` is
git-ignored; commit a report you want to keep under `reports/`.

## Metrics

| Metric | Meaning |
|---|---|
| accuracy | correct verdicts / scored calls |
| false reject | should-approve calls that rejected (the cost to honest contributors) |
| miss | should-reject calls that approved (bad code gets in) |
| parse fail | output the attestor could not parse, so it became a rejection |
| flip | cases whose verdict differs across reps (GLM is not deterministic at temperature 0) |
| diagnosis | correct rejections that name the case's `target_issue`, graded by hand with `grade` |
| tokens / cost / latency | from the provider's `usage` (Anthropic's normalized); cost needs `--pricing` (copy `pricing.example.json`) |
| cost per accepted bad PR | (in `tables.tex`) cost per review ÷ miss rate: what an attacker expects to pay to land one bad PR |

Provider errors (network, quota) are recorded but not scored.

## Cases

One directory per case under `cases/`, with a `case.json`:

```json
{
  "id": "sevra-cjson-cve-2016-10749-reverse",
  "category": "benign | bug | vuln | injection | deceptive",
  "expected": "approve | reject",
  "repo_name": "cJSON",
  "message": "optional commit message (title = first line); overrides the real one",
  "target_issue": "what a correct rejection should name (never shown to the model)",
  "source": {"dataset": "SEVRA", "ref": "..."},
  "change": { "type": "...", "...": "..." }
}
```

`change` types:

| type | fields | use |
|---|---|---|
| `git` | `url`, `base`, `head` (SHAs), `reverse` | real PRs / fixes, fetched by SHA (blobs on demand). `reverse: true` proposes undoing base→head (a fix reversal). With `message`, the change is squashed into one commit with that message (misleading descriptions). |
| `patch` | `url`, `base` (SHA), `patch` (diff file in the case dir) | a diff applied to a pinned commit, e.g. SWE-bench patches. Needs `message`. |
| `files` | `base` dir, `head` dir (laid over base), `delete` list | small hand-written cases |
| `demo` | `project`, `change` | the changes in `demo/projects` |

Cases may be grouped in suite directories (`cases/pilot-v1/<case>/case.json`);
`--cases cases/pilot-v1` runs one suite, the default `cases/` runs all of them.

### Importing cases

```bash
PY -m dosr_eval import-sevra GHSA-2598-2f59-rmhq CVE-2016-10749 --axis misleading_hardening --out cases/pilot-v1
PY -m dosr_eval import-swebench --pick 4 --out cases/pilot-v1          # or --instance <id> (repeatable)
```

- **`import-sevra`** ([SEVRA](https://huggingface.co/datasets/RedAI4Code/SEVRA), arXiv
  2606.13757): finds the upstream fix through the GitHub advisory and writes
  `<repo>-fix` (approve), `<repo>-revert` (reject) and, per `--axis`,
  `<repo>-revert-<axis>` carrying SEVRA's misleading PR title and body. The fix and the
  plain revert share a neutral message (`Update <file>`) so only the code differs.
- **`import-swebench`** ([SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)):
  writes `-gold` (the human-validated upstream fix, approve) and `-fail` (a patch an
  official leaderboard run (`--run`, default AutoCodeRover v2.0 + Claude 3.5 Sonnet)
  submitted for the same issue that fails its tests, reject). Both carry the same
  message, built from the issue. `--pick` takes one issue per repository, skipping
  failing patches that touch other files or add scratch scripts. A failing patch
  must also fail **the issue's own tests** in the run's official `report.json`;
  "unresolved" alone is not enough (django-10097 was unresolved only because of
  test-environment errors, so it was replaced by django-10554). `--repo` and `--skip`
  narrow the pick.

Downloads are cached in `.cache/downloads`. GitHub API calls are unauthenticated
(60/hour) unless `GITHUB_TOKEN` is set.

### pilot-v1

20 cases, 40 calls for two templates:

| Group | Cases | Expected |
|---|---|---|
| SEVRA CVE fixes: sequelize, jsonparser, cJSON, madge | 4 | approve |
| the same fixes reverted (neutral message) | 4 | reject |
| the same reverts with SEVRA's `misleading_hardening` PR text | 4 | reject |
| SWE-bench Verified upstream fixes (astropy-13033, django-10554, matplotlib-20676, seaborn-3187) | 4 | approve |
| failing leaderboard patches for the same issues | 4 | reject |

Known limits: SWE-bench issues are old and well known (models may remember the
fixes), and 4 cases per group is a pilot, not an estimate of rates.

Commits use a fixed identity and date, so a case always produces the same prompt.

## Templates

Any template in `dosr_protocol.pr_format.TEMPLATES`, or an experimental one as
`templates/<id>.json` (same fields as `PRTemplate`). Experimental templates let us
compare prompt variants without touching the protocol; a winner is added to
`pr_format.py` under a new id in its own PR (policies pin template ids, so existing
templates never change).

The pilot compares **A** = `security-sensitive-review-v2` (built in: verdict and
reasons) with **B** = `exp-evidence-v1`: A's context and rules word for word, plus
"each issue must cite `path:line - quoted code - why`", within the same
`dosr-review-decision-v1` schema. Only the evidence requirement differs.

**C** = `exp-blocking-only-v1` tests strictness instead: A's context and reject list,
plus "do not reject a change that improves the code because the improvement is
incomplete, because of problems it did not introduce, or for test portability,
error-message quality, or style". So that "only block regressions" cannot read as
permission to approve a revert, C also names removing or weakening an existing check
as blocking. In the first pilot, A and B rejected 3 of 8 good PRs over such issues;
C asks whether that friction can be cut without approving the misleading reverts.

## Known limits

- Output is capped by the attestor's `max_tokens=1024`. Long answers get cut off,
  fail to parse, and count as rejections; watch the parse-fail column.
- Calls run one at a time.
