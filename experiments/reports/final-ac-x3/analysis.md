# Additional analyses (final-v1)

Excluded everywhere: the astropy-8872 and pylint-4604 pairs. Their official SWE-bench failures
cannot show the patch is wrong (astropy: the test module fails to collect in the benchmark
environment, identically for other leaderboard systems; pylint: the tests import a helper only
the upstream fix adds). Ground truth per case: `swe-failure-reasons.md`.

## Call-site context (L2) vs. today's context (L1)

SWE-bench Verified cases, template C (`exp-blocking-only-v1`), 3 runs each. L1 = main runs, L2 = `--context callers` runs.

| Model | Group | L1 correct | L2 correct | L1 prompt tok/call | L2 prompt tok/call |
|---|---|---|---|---|---|
| gpt-6-luna | Upstream fix (approve) | 27/27 (100%, CI 88%–100%) | 25/27 (93%, CI 77%–98%) | 10,670 | 12,371 |
| gpt-6-luna | Failing patch: broke other code | 10/12 (83%, CI 55%–95%) | 9/12 (75%, CI 47%–91%) | 13,627 | 15,242 |
| gpt-6-luna | Failing patch: didn't fix it | 6/15 (40%, CI 20%–64%) | 4/15 (27%, CI 11%–52%) | 8,259 | 11,042 |
| claude-haiku-5-5 | Upstream fix (approve) | 26/27 (96%, CI 82%–99%) | 27/27 (100%, CI 88%–100%) | 16,657 | 19,269 |
| claude-haiku-5-5 | Failing patch: broke other code | 12/12 (100%, CI 76%–100%) | 10/12 (83%, CI 55%–95%) | 21,364 | 23,856 |
| claude-haiku-5-5 | Failing patch: didn't fix it | 8/15 (53%, CI 30%–75%) | 7/15 (47%, CI 25%–70%) | 12,789 | 17,096 |

Result: call-site context gives no consistent improvement. Only Haiku's upstream-fix group moves up
(26/27 → 27/27); the other five groups are equal or lower, including the "broke other code" patches
the context was meant to help with. All differences are within the 95% intervals, at 15–35% more
prompt tokens. Consistent with SWE-PRBench (more context did not help review).

## Haiku parse failures: official vs. intended verdict

The attestor counts unparseable output as a rejection (Anthropic models get no structured-output mode).
'Intended' reads `"approved": true/false` from the raw text (analysis only).

| Template | Parse fails | Accuracy official / intended | False reject official / intended | Miss official / intended |
|---|---|---|---|---|
| security-sensitive-review-v2 | 21 | 91% / 92% | 29% / 26% | 3% / 3% |
| exp-blocking-only-v1 | 22 | 91% / 92% | 15% / 10% | 7% / 7% |
