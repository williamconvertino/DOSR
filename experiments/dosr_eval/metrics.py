"""Scores a results JSONL file.

A call's verdict is "approve" or "reject"; unparseable output counts as "reject"
(as in the attestor). Calls that errored (network, quota) are counted but scored
as neither.

- accuracy          correct verdicts / scored calls
- false_reject      share of calls on should-approve cases that rejected
- miss              share of calls on should-reject cases that approved
- parse_fail        share of scored calls whose output did not parse
- flip              share of cases (with 2+ scored reps) whose verdicts disagree
"""

import json
from collections import defaultdict
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]


def verdict(row: dict) -> str | None:
    if "error" in row:
        return None
    return "approve" if row.get("approved") else "reject"


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if verdict(r) is not None]
    should_approve = [r for r in scored if r["expected"] == "approve"]
    should_reject = [r for r in scored if r["expected"] == "reject"]

    by_case = defaultdict(set)
    reps = defaultdict(int)
    for r in scored:
        by_case[r["case"]].add(verdict(r))
        reps[r["case"]] += 1
    multi = [c for c in by_case if reps[c] >= 2]

    usage = [r["usage"] for r in scored if r.get("usage")]
    costs = [r["cost"] for r in scored if r.get("cost") is not None]
    latencies = [r["latency_ms"] for r in scored if r.get("latency_ms") is not None]
    return {
        "calls": len(rows),
        "errors": len(rows) - len(scored),
        "accuracy": _rate(sum(verdict(r) == r["expected"] for r in scored), len(scored)),
        "false_reject": _rate(sum(verdict(r) == "reject" for r in should_approve), len(should_approve)),
        "miss": _rate(sum(verdict(r) == "approve" for r in should_reject), len(should_reject)),
        "parse_fail": _rate(sum(bool(r.get("parse_error")) for r in scored), len(scored)),
        "flip": _rate(sum(len(by_case[c]) > 1 for c in multi), len(multi)),
        "mean_prompt_tokens": _rate(sum(u.get("prompt_tokens", 0) for u in usage), len(usage)),
        "mean_completion_tokens": _rate(sum(u.get("completion_tokens", 0) for u in usage), len(usage)),
        "total_cost": sum(costs) if costs else None,
        "mean_latency_ms": _rate(sum(latencies), len(latencies)),
    }


def group(rows: list[dict], *keys: str) -> dict[tuple, list[dict]]:
    out = defaultdict(list)
    for r in rows:
        out[tuple(r[k] for k in keys)].append(r)
    return dict(sorted(out.items()))


def _fmt(v, kind: str = "pct") -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "cost":
        return f"{v:.4f}"
    return f"{v:,.0f}"


def report(rows: list[dict]) -> str:
    lines = ["## By template and model", "",
             "| Template | Model | Calls | Errors | Accuracy | False reject | Miss | Parse fail | Flip "
             "| Prompt tok | Output tok | Cost | Latency ms |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (tid, model), rs in group(rows, "template", "model").items():
        s = summarize(rs)
        lines.append(
            f"| {tid} | {model} | {s['calls']} | {s['errors']} | {_fmt(s['accuracy'])} | "
            f"{_fmt(s['false_reject'])} | {_fmt(s['miss'])} | {_fmt(s['parse_fail'])} | {_fmt(s['flip'])} | "
            f"{_fmt(s['mean_prompt_tokens'], 'n')} | {_fmt(s['mean_completion_tokens'], 'n')} | "
            f"{_fmt(s['total_cost'], 'cost')} | {_fmt(s['mean_latency_ms'], 'n')} |")

    lines += ["", "## Accuracy by category", "", "| Template | Model | Category | Calls | Accuracy |",
              "|---|---|---|---|---|"]
    for (tid, model, cat), rs in group(rows, "template", "model", "category").items():
        lines.append(f"| {tid} | {model} | {cat} | {len(rs)} | {_fmt(summarize(rs)['accuracy'])} |")

    wrong = [r for r in rows if verdict(r) is not None and verdict(r) != r["expected"]]
    if wrong:
        lines += ["", "## Wrong verdicts", "", "| Case | Template | Model | Rep | Expected | Got | Why |",
                  "|---|---|---|---|---|---|---|"]
        for r in wrong:
            why = r.get("parse_error") or (r.get("summary") or "")
            why = why.replace("|", "\\|").replace("\n", " ")[:160]
            lines.append(f"| {r['case']} | {r['template']} | {r['model']} | {r['rep']} | {r['expected']} | "
                         f"{verdict(r)} | {why} |")
    return "\n".join(lines) + "\n"
