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
demo/.venv/bin/python -m pip install -e "experiments[dev]"
cd experiments
../demo/.venv/bin/python -m pytest      # offline, no API key needed
```

Real models use the contributor's key, as in DOSR: `ZHIPU_API_KEY` in the repo's
`.env` (git-ignored) or the environment. Other OpenAI-compatible providers: pass
`--providers-file` (attestor format) and set `<PROVIDER>_API_KEY`.

## Commands

Run from `experiments/` (`PY` = the venv's Python):

```bash
PY -m dosr_eval list                                   # cases and their labels
PY -m dosr_eval prompt demo-calc-evaluate-injection    # exact prompt sent to the model
PY -m dosr_eval run --dry-run \
   --template security-sensitive-review-v2 --template exp-quality-bar-v0 \
   --model zhipu/glm-4.6@api-v4 --reps 3                # calls, tokens, worst-case cost; no calls made
PY -m dosr_eval run --template security-sensitive-review-v2 --template exp-quality-bar-v0 \
   --model zhipu/glm-4.6@api-v4 --reps 3 --budget 0.50 --label pilot
PY -m dosr_eval report results/<run>.jsonl             # re-score a finished run
PY -m dosr_eval figures results/<run>.jsonl            # paper tables + charts -> reports/figures/<run>/
PY -m dosr_eval spend --since 2026-10-20               # API spend across all runs
```

Defaults: template `security-sensitive-review-v2`, model `mock/deterministic-reviewer-v1@1`
(approves everything; for plumbing checks only).

Each run writes `results/<run_id>.jsonl` (one row per model call) and
`results/<run_id>.meta.json` (cases, templates, models, reps, DOSR commit). `results/` is
git-ignored; commit a report you want to keep under `reports/`.

## Spend

Copy `pricing.example.json` to `pricing.json` (git-ignored; used by default). It has
Z.ai's list price for GLM-4.6; the attestor calls bigmodel.cn, which bills in CNY, so
check your account's billing page.

- `run --dry-run` prints a worst-case cost: the prompt estimate ×1.5 plus a full
  1024-token answer (the attestor's `max_tokens`) for every call.
- `run --budget X` stops before a call whose worst case would take total spend past X.
  The prompt side is an estimate, so treat the cap as close, not exact.
- `spend` totals tokens and cost per run and model from `results/*.jsonl`, pricing any
  row recorded without a price.

## Paper figures

`figures RESULTS.jsonl [...]` writes, per run:

| File | Content |
|---|---|
| `matrix.tex` / `.md` | accept and reject rate per template × cell (PR quality high/low × code good/bad); the required outcome in bold |
| `rates.tex` / `.md` | accuracy, false reject, miss, parse fail, flip per template |
| `cost.tex` / `.md` | tokens, cost and latency per review |
| `accept_by_cell.pdf` / `.png` | accept rate per cell, one bar per template |
| `rates.pdf` / `.png` | false reject, miss, parse fail per template |

Every rate carries a 95% Wilson interval, because pilot samples are small. Runs made
only with the mock reviewer are labeled "placeholder data" in every caption.
The LaTeX tables need `\usepackage{booktabs,multirow}`.

## Metrics

| Metric | Meaning |
|---|---|
| accuracy | correct verdicts / scored calls |
| false reject | should-approve calls that rejected (the cost to honest contributors) |
| miss | should-reject calls that approved (bad code gets in) |
| parse fail | output the attestor could not parse, so it became a rejection |
| flip | cases whose verdict differs across reps (GLM is not deterministic at temperature 0) |
| tokens / cost / latency | from the provider's `usage`; cost needs `--pricing` (copy `pricing.example.json`) |

Provider errors (network, quota) are recorded but not scored. Diagnosis (did the model
name the `target_issue`?) is not scored automatically yet: each row keeps `summary` and
`issues` for grading.

## Cases

One directory per case under `cases/`, with a `case.json`:

```json
{
  "id": "sevra-cjson-cve-2016-10749-reverse",
  "category": "benign | bug | vuln | injection | deceptive",
  "expected": "approve | reject",
  "repo_name": "cJSON",
  "quality": "high | low   (optional: description, tests, clarity)",
  "code": "good | bad      (optional: correct and safe, or not)",
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
| `files` | `base` dir, `head` dir (laid over base), `delete` list | small hand-written cases |
| `demo` | `project`, `change` | the changes in `demo/projects` |

Commits use a fixed identity and date, so a case always produces the same prompt.

`quality` + `code` place a case in the evaluation matrix. Policy: only a high-quality
PR with good code is accepted, so the loader rejects a case whose `expected` disagrees.
Build matrix cells as **pairs**: the same code with a good and a poor presentation
(`message`, and `delete` to drop tests), so a verdict change can only come from
presentation (see `demo-calc-add-power` / `demo-calc-add-power-low-quality`).

## Templates

Any built-in template (`dosr_protocol/templates/*.toml`), or an experimental one as
`templates/<id>.toml` in the same format (`dosr template new` makes a starting copy).
A winner can be embedded in a repository policy as `review.template` unchanged, or
added as a built-in under a new id (policies pin ids, so templates never change).

| Experimental template | Tests |
|---|---|
| `exp-evidence-v0` | each issue must cite `path:line - code - reason` (Jakub's template B) |
| `exp-quality-bar-v0` | security-v2 plus a quality bar: clear message, tests for new behavior |

## Known limits

- Output is capped by the attestor's `max_tokens=1024`. Long answers get cut off,
  fail to parse, and count as rejections; watch the parse-fail column.
- Calls run one at a time.
