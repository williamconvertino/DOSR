## Latency and cost by change size

| Model | Lines changed | Cases | Calls | Latency median (s) | Latency p90 (s) | Prompt tok (median) | Output tok (median) | Cost / review (mean, USD) |
|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 1–10 | 63 | 378 | 4.6 | 7.0 | 8,448 | 839 | 0.0017 |
| claude-haiku-5-5 | 11–50 | 59 | 353 | 6.4 | 9.0 | 14,799 | 1,189 | 0.0029 |
| gpt-6-luna | 1–10 | 63 | 378 | 4.2 | 8.0 | 5,401 | 266 | 0.0009 |
| gpt-6-luna | 11–50 | 59 | 353 | 5.3 | 9.7 | 9,215 | 379 | 0.0016 |

## What drives latency and cost

Spearman rank correlation (1 = perfectly monotonic).

| Model | Calls | lines→latency | lines→cost | lines→prompt tok | file bytes→prompt tok | prompt tok→latency | output tok→latency | s per +1k prompt tok | cost per 1k prompt tok (USD) |
|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 731 | 0.49 | 0.35 | 0.24 | 0.36 | 0.37 | 0.97 | 0.06 | 0.00013 |
| gpt-6-luna | 731 | 0.27 | 0.29 | 0.22 | 0.36 | 0.11 | 0.94 | 0.01 | 0.00012 |
