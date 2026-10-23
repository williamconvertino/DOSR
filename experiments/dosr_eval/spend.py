"""API spend: up-front worst-case estimates, and totals across finished runs.

Every results row already carries the provider's token `usage`, so spend is summed
from results/*.jsonl; there is no separate ledger to keep in sync. A row without a
recorded cost (no pricing at run time) is priced with the current pricing file.
"""

import json
from collections import defaultdict
from pathlib import Path

# The attestor's ChatCompletionsReviewer sends max_tokens=1024, so no call can bill
# more output than this.
MAX_OUTPUT_TOKENS = 1024


def price_of(pricing: dict, model: str) -> dict | None:
    p = pricing.get(model)
    if not isinstance(p, dict) or p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
        return None
    return p


def cost_of(usage: dict | None, price: dict | None) -> float | None:
    if not usage or not price:
        return None
    return (usage.get("prompt_tokens", 0) * price["input_per_mtok"]
            + usage.get("completion_tokens", 0) * price["output_per_mtok"]) / 1_000_000


def worst_case_call(prompt_est_tokens: int, price: dict) -> float:
    """Upper-ish bound for one call: estimated prompt + the full output allowance.
    The prompt side is the chars/4 estimate, so pad it."""
    return (prompt_est_tokens * 1.5 * price["input_per_mtok"]
            + MAX_OUTPUT_TOKENS * price["output_per_mtok"]) / 1_000_000


def summarize_spend(results_dir: Path, pricing: dict, since: str | None = None) -> list[dict]:
    """One entry per (run, model), oldest first. `since` is an ISO date (YYYY-MM-DD)."""
    acc = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0,
                               "unpriced_calls": 0})
    for path in sorted(Path(results_dir).glob("*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            day = r["run_id"][:8]
            if since and day < since.replace("-", ""):
                continue
            a = acc[(r["run_id"], r["model"])]
            a["calls"] += 1
            usage = r.get("usage") or {}
            a["prompt_tokens"] += usage.get("prompt_tokens", 0)
            a["completion_tokens"] += usage.get("completion_tokens", 0)
            cost = r.get("cost")
            if cost is None:
                cost = cost_of(usage, price_of(pricing, r["model"]))
            if cost is None:
                if usage:  # a real call we cannot price (mock calls have no usage)
                    a["unpriced_calls"] += 1
            else:
                a["cost"] += cost
    return [{"run_id": run, "model": model, **a} for (run, model), a in sorted(acc.items())]


def spend_report(entries: list[dict], pricing: dict) -> str:
    lines = ["| Run | Model | Calls | Prompt tok | Output tok | Cost | Unpriced calls |",
             "|---|---|---|---|---|---|---|"]
    totals = defaultdict(float)
    for e in entries:
        currency = (pricing.get(e["model"]) or {}).get("currency", "")
        lines.append(f"| {e['run_id']} | {e['model']} | {e['calls']} | {e['prompt_tokens']:,} | "
                     f"{e['completion_tokens']:,} | {e['cost']:.4f} {currency} | {e['unpriced_calls']} |")
        totals[currency] += e["cost"]
    lines.append("")
    lines.append("Total: " + (", ".join(f"{v:.4f} {k}".strip() for k, v in totals.items()) or "0"))
    if any(e["unpriced_calls"] for e in entries):
        lines.append("Some calls have no price: add their model to pricing.json.")
    return "\n".join(lines) + "\n"
