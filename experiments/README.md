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
   --template security-sensitive-review-v2 --template exp-example-evidence-v0 \
   --model zhipu/glm-4.6@api-v4 --reps 3                # call count + token estimate, no calls
PY -m dosr_eval run --model zhipu/glm-4.6@api-v4 --reps 3 --pricing pricing.json --label pilot
PY -m dosr_eval report results/<run>.jsonl             # re-score a finished run
```

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

## Templates

Any template in `dosr_protocol.pr_format.TEMPLATES`, or an experimental one as
`templates/<id>.json` (same fields as `PRTemplate`). Experimental templates let us
compare prompt variants without touching the protocol; a winner is added to
`pr_format.py` under a new id in its own PR (policies pin template ids, so existing
templates never change). `exp-example-evidence-v0` shows the shape: each issue must
cite file, evidence, and reason, within the existing `dosr-review-decision-v1` schema.

## Known limits

- Output is capped by the attestor's `max_tokens=1024`. Long answers get cut off,
  fail to parse, and count as rejections; watch the parse-fail column.
- Calls run one at a time.
