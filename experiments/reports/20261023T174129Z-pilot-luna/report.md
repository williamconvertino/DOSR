## By template and model

| Template | Model | Calls | Errors | Accuracy | False reject | Miss | Parse fail | Flip | Diagnosis | Prompt tok | Output tok | Cost | Latency ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| exp-evidence-v1 | openai/gpt-6-luna@latest | 19 | 0 | 84% | 38% | 0% | 0% | — | 91% | 12,860 | 439 | 0.0286 | 6,550 |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | 19 | 0 | 84% | 38% | 0% | 0% | — | 91% | 12,787 | 426 | 0.0283 | 7,164 |

## Correct verdicts by case group

| Template | Model | Group | Expected | Calls | Correct |
|---|---|---|---|---|---|
| exp-evidence-v1 | openai/gpt-6-luna@latest | CVE fix | approve | 4 | 50% |
| exp-evidence-v1 | openai/gpt-6-luna@latest | CVE fix reverted | reject | 4 | 100% |
| exp-evidence-v1 | openai/gpt-6-luna@latest | Reverted, misleading text | reject | 4 | 100% |
| exp-evidence-v1 | openai/gpt-6-luna@latest | Upstream fix | approve | 4 | 75% |
| exp-evidence-v1 | openai/gpt-6-luna@latest | Failing agent patch | reject | 3 | 100% |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | CVE fix | approve | 4 | 25% |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | CVE fix reverted | reject | 4 | 100% |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | Reverted, misleading text | reject | 4 | 100% |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | Upstream fix | approve | 4 | 100% |
| security-sensitive-review-v2 | openai/gpt-6-luna@latest | Failing agent patch | reject | 3 | 100% |

## Wrong verdicts

| Case | Template | Model | Rep | Expected | Got | Why |
|---|---|---|---|---|---|---|
| sevra-cjson-fix | security-sensitive-review-v2 | openai/gpt-6-luna@latest | 0 | approve | reject | The change prevents the out-of-bounds write for a string ending in a backslash, but it returns from parse_string without setting the error pointer. As a result, |
| sevra-cjson-fix | exp-evidence-v1 | openai/gpt-6-luna@latest | 0 | approve | reject | The added check prevents reading beyond a trailing backslash, but returns without setting the parser error pointer. As a result, malformed strings ending in a b |
| sevra-jsonparser-fix | security-sensitive-review-v2 | openai/gpt-6-luna@latest | 0 | approve | reject | The new validation runs too late to protect against malformed array-index path segments. The condition reads keys[level][0] before checking that the segment is  |
| sevra-jsonparser-fix | exp-evidence-v1 | openai/gpt-6-luna@latest | 0 | approve | reject | The new validation does not prevent a panic for an empty path component when searching an array: the condition indexes the component before the length check can |
| sevra-madge-fix | security-sensitive-review-v2 | openai/gpt-6-luna@latest | 0 | approve | reject | Using execFile with a separate argument array avoids shell parsing and is an improvement, but the updated test hard-codes a Unix-specific spawn error and exact  |
| swe-mwaskom-seaborn-3187-gold | exp-evidence-v1 | openai/gpt-6-luna@latest | 0 | approve | reject | The change addresses legend formatting, but it also mutates the formatter attached to real plot axes while constructing the legend. This changes axis tick label |
