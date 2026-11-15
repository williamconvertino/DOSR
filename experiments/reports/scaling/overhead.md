## DOSR overhead by change size (mock reviewer, in-process attestor)

| Lines changed | Files | Patch KB | Prompt tok (est) | Client: commit, bundle, build PR + prompt | Attestor: upload, verify, review (mock), sign | Client: verify attestation, register on chain | Total (ms) |
|---|---|---|---|---|---|---|---|
| 10 | 1 | 1.1 | 4,298 | 332 | 248 | 8 | 586 |
| 30 | 1 | 2.5 | 4,991 | 433 | 200 | 8 | 648 |
| 100 | 1 | 6.6 | 7,070 | 461 | 292 | 16 | 814 |
| 300 | 1 | 18.8 | 13,296 | 394 | 231 | 8 | 616 |
| 1,000 | 4 | 63.5 | 46,234 | 459 | 264 | 8 | 708 |
| 3,000 | 12 | 190.6 | 138,079 | 686 | 365 | 8 | 1,073 |

Median of the repetitions per step. Excludes network transfer, the review model, a real chain, and IPFS publishing.
