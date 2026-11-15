## Projection for larger changes (estimate, not measured)

Prompt size: built by DOSR for a synthetic change (overhead benchmark), calibrated to billed tokens. Cost: list price incl. long-prompt tiers, with the model's median output. Latency: linear fit on the real calls.

| Model | Lines changed | Prompt tok (billed, est.) | Latency (s, est.) | Cost / review (USD, est.) | Note |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 10 | 7,151 | 5.4 | 0.0012 | within measured range |
| claude-haiku-5-5 | 30 | 8,304 | 5.4 | 0.0013 | within measured range |
| claude-haiku-5-5 | 100 | 11,762 | 5.6 | 0.0017 | within measured range |
| claude-haiku-5-5 | 300 | 22,121 | 6.2 | 0.0027 | within measured range |
| claude-haiku-5-5 | 1,000 | 76,920 | 9.2 | 0.0082 | within measured range |
| claude-haiku-5-5 | 3,000 | 229,724 | 17.7 | 0.1174 | extrapolated, long-prompt rate |
| gpt-6-luna | 10 | 4,556 | 5.3 | 0.0006 | within measured range |
| gpt-6-luna | 30 | 5,290 | 5.3 | 0.0007 | within measured range |
| gpt-6-luna | 100 | 7,494 | 5.3 | 0.0009 | within measured range |
| gpt-6-luna | 300 | 14,093 | 5.4 | 0.0016 | within measured range |
| gpt-6-luna | 1,000 | 49,005 | 5.9 | 0.0051 | extrapolated |
| gpt-6-luna | 3,000 | 146,355 | 7.3 | 0.0148 | extrapolated |
