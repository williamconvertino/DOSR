"""How review latency and cost grow with the size of a change.

Joins finished results rows (one per model call: latency, token usage, cost) with the
size of each case's change, measured from Git (files, lines added + deleted, patch
bytes, and the bytes of the changed files at the candidate, which the templates
include in full). No model calls are made.

Outputs (one directory):
  sizes.csv               one row per case: change size
  scaling.csv             one row per call: size joined to latency, tokens, cost
  scaling.md / .tex       per model x size bucket: median/p90 latency, tokens, cost
  latency_vs_size.pdf/png review latency vs lines changed (one series per model)
  cost_vs_size.pdf/png    cost per review vs lines changed
  tokens_vs_size.pdf/png  prompt tokens vs lines changed vs changed-file size
"""

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from .cases import load_cases, materialize
from .figures import GRID, INK, INK_MUTED, SERIES_COLORS
from .spend import cost_of

# Lines changed (added + deleted) buckets, inclusive upper bounds.
BUCKETS = ((1, 10), (11, 50), (51, 200), (201, 1000), (1001, math.inf))
MARKERS = ["o", "s", "^", "D"]


def bucket_of(lines: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= lines <= hi:
            return f"{lo}–{hi}" if hi != math.inf else f"{lo}+"
    return "0"


def bucket_order() -> list[str]:
    return [f"{lo}–{hi}" if hi != math.inf else f"{lo}+" for lo, hi in BUCKETS]


# ---------------------------------------------------------------- sizes

def measure_sizes(cases_dir: Path, case_ids: set[str], work: Path, cache: Path, log=print) -> dict[str, dict]:
    sizes = {}
    for case in load_cases(cases_dir):
        if case.id not in case_ids:
            continue
        m = materialize(case, work, cache)
        files = m.git.changed_files(m.parent, m.candidate)
        changed_bytes = 0
        for f in files:
            if f["status"] != "D":
                data = m.git.run_bytes("cat-file", "-s", f"{m.candidate}:{f['path']}", check=False).strip()
                changed_bytes += int(data or 0)
        sizes[case.id] = {
            "case": case.id,
            "files": len(files),
            "added": sum(f["additions"] or 0 for f in files),
            "deleted": sum(f["deletions"] or 0 for f in files),
            "patch_bytes": len(m.git.diff(m.parent, m.candidate).encode("utf-8")),
            "changed_file_bytes": changed_bytes,
        }
        sizes[case.id]["lines"] = sizes[case.id]["added"] + sizes[case.id]["deleted"]
        log(f"  {case.id}: {sizes[case.id]['lines']} lines, {len(files)} file(s)")
    return sizes


def load_or_measure(sizes_csv: Path, cases_dir: Path, case_ids: set[str], work: Path, cache: Path,
                    log=print) -> dict[str, dict]:
    sizes = {}
    if sizes_csv.is_file():
        with sizes_csv.open(newline="") as f:
            sizes = {r["case"]: {k: (v if k == "case" else int(v)) for k, v in r.items()} for r in csv.DictReader(f)}
    missing = case_ids - set(sizes)
    if missing:
        log(f"measuring {len(missing)} case(s) from Git...")
        sizes.update(measure_sizes(cases_dir, missing, work, cache, log))
    return sizes


# ---------------------------------------------------------------- join + stats

def join(rows: list[dict], sizes: dict[str, dict]) -> list[dict]:
    out = []
    for r in rows:
        if "error" in r or r["case"] not in sizes:
            continue
        usage = r.get("usage") or {}
        s = sizes[r["case"]]
        out.append({
            "case": r["case"], "model": r["model"], "template": r["template"],
            "context": r.get("context", "none"),
            "lines": s["lines"], "files": s["files"], "patch_bytes": s["patch_bytes"],
            "changed_file_bytes": s["changed_file_bytes"], "bucket": bucket_of(s["lines"]),
            "latency_ms": r.get("latency_ms"), "prompt_est_tokens": r.get("prompt_est_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cost": r.get("cost"),
        })
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:
        return None


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares slope and intercept."""
    if len(xs) < 2:
        return None
    try:
        slope, intercept = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:
        return None
    return slope, intercept


def bucket_table(joined: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for r in joined:
        groups[(r["model"], r["bucket"])].append(r)
    out = []
    for model in sorted({r["model"] for r in joined}):
        for b in bucket_order():
            rs = groups.get((model, b), [])
            if not rs:
                continue
            lat = [r["latency_ms"] / 1000 for r in rs if r["latency_ms"] is not None]
            pt = [r["prompt_tokens"] for r in rs if r["prompt_tokens"] is not None]
            ct = [r["completion_tokens"] for r in rs if r["completion_tokens"] is not None]
            cost = [r["cost"] for r in rs if r["cost"] is not None]
            out.append({
                "model": model, "bucket": b, "calls": len(rs), "cases": len({r["case"] for r in rs}),
                "latency_median_s": _pct(lat, 0.5), "latency_p90_s": _pct(lat, 0.9),
                "prompt_tokens_median": _pct(pt, 0.5), "completion_tokens_median": _pct(ct, 0.5),
                "cost_mean": statistics.fmean(cost) if cost else None,
                "cost_p90": _pct(cost, 0.9),
            })
    return out


def model_summary(joined: list[dict]) -> list[dict]:
    out = []
    for model in sorted({r["model"] for r in joined}):
        rs = [r for r in joined if r["model"] == model and r["latency_ms"] is not None and r["prompt_tokens"]]
        lines = [r["lines"] for r in rs]
        lat = [r["latency_ms"] / 1000 for r in rs]
        pt = [r["prompt_tokens"] for r in rs]
        ct = [r["completion_tokens"] or 0 for r in rs]
        cost = [r["cost"] for r in rs if r["cost"] is not None]
        lat_fit = linear_fit(pt, lat)
        priced = [r for r in rs if r["cost"] is not None]
        cost_fit = linear_fit([r["prompt_tokens"] for r in priced], [r["cost"] for r in priced])
        calib = [r["prompt_tokens"] / r["prompt_est_tokens"] for r in rs if r.get("prompt_est_tokens")]
        out.append({
            "model": model, "calls": len(rs),
            "rho_lines_latency": spearman(lines, lat),
            "rho_lines_cost": spearman([r["lines"] for r in rs if r["cost"] is not None], cost),
            "rho_lines_prompt_tokens": spearman(lines, pt),
            "rho_filebytes_prompt_tokens": spearman([r["changed_file_bytes"] for r in rs], pt),
            "rho_prompt_tokens_latency": spearman(pt, lat),
            "rho_completion_tokens_latency": spearman(ct, lat),
            "latency_s_per_1k_prompt_tokens": None if lat_fit is None else lat_fit[0] * 1000,
            "latency_s_intercept": None if lat_fit is None else lat_fit[1],
            "cost_per_1k_prompt_tokens": (sum(cost) / sum(pt) * 1000) if cost and len(cost) == len(rs) else None,
            "cost_mean": statistics.fmean(cost) if cost else None,
            "latency_median_s": _pct(lat, 0.5),
            "cost_usd_per_1k_prompt_tokens_fit": None if cost_fit is None else cost_fit[0] * 1000,
            "cost_usd_intercept_fit": None if cost_fit is None else cost_fit[1],
            # Real tokens per harness estimate (chars/4), to turn estimates into billed tokens.
            "token_calibration": statistics.median(calib) if calib else None,
            "prompt_tokens_min": min(pt) if pt else None, "prompt_tokens_max": max(pt) if pt else None,
            "completion_tokens_median": _pct(ct, 0.5),
        })
    return out


def projection(summary: list[dict], est_tokens_by_lines: dict[int, int], pricing: dict) -> tuple[str, list[dict]]:
    """Estimated latency and cost for change sizes beyond the measured suite.

    Prompt tokens: the exact prompt DOSR builds for a synthetic change of that size
    (overhead benchmark, chars/4), calibrated to billed tokens with the real calls.
    Cost: those tokens plus the model's median output, at list price (including any
    long-prompt tier). Latency: the linear fit of latency on prompt tokens from the real
    calls. Past the largest measured prompt, everything is an extrapolation."""
    rows = []
    for s in summary:
        price = pricing.get(s["model"])
        if None in (s["token_calibration"], s["latency_s_per_1k_prompt_tokens"]) or not price:
            continue
        for lines, est in sorted(est_tokens_by_lines.items()):
            tok = est * s["token_calibration"]
            rows.append({
                "model": s["model"], "lines": lines, "prompt_tokens": tok,
                "latency_s": s["latency_s_intercept"] + s["latency_s_per_1k_prompt_tokens"] * tok / 1000,
                "cost_usd": cost_of({"prompt_tokens": tok, "completion_tokens": s["completion_tokens_median"]}, price),
                "long_prompt_rate": cost_of({"prompt_tokens": tok, "completion_tokens": 0}, price)
                > tok * price["input_per_mtok"] / 1e6 + 1e-12,
                "measured_range": s["prompt_tokens_min"] <= tok <= s["prompt_tokens_max"],
            })
    md = ["## Projection for larger changes (estimate, not measured)", "",
          "Prompt size: built by DOSR for a synthetic change (overhead benchmark), calibrated to billed tokens. "
          "Cost: list price incl. long-prompt tiers, with the model's median output. Latency: linear fit on the "
          "real calls.", "",
          "| Model | Lines changed | Prompt tok (billed, est.) | Latency (s, est.) | Cost / review (USD, est.) | Note |",
          "|---|---|---|---|---|---|"]
    for r in rows:
        note = [] if r["measured_range"] else ["extrapolated"]
        if r["long_prompt_rate"]:
            note.append("long-prompt rate")
        md.append(f"| {r['model'].split('/')[1].split('@')[0]} | {r['lines']:,} | {r['prompt_tokens']:,.0f} | "
                  f"{r['latency_s']:.1f} | {r['cost_usd']:.4f} | {', '.join(note) or 'within measured range'} |")
    return "\n".join(md) + "\n", rows


# ---------------------------------------------------------------- tables

def _n(v, fmt="{:,.0f}", none="—"):
    return none if v is None else fmt.format(v)


def tables(buckets: list[dict], summary: list[dict]) -> tuple[str, str]:
    md = ["## Latency and cost by change size", "",
          "| Model | Lines changed | Cases | Calls | Latency median (s) | Latency p90 (s) | Prompt tok (median) "
          "| Output tok (median) | Cost / review (mean, USD) |",
          "|---|---|---|---|---|---|---|---|---|"]
    tex = ["\\begin{table}[t]", "\\centering", "\\small", "\\begin{tabular}{llrrrrrr}", "\\toprule",
           "Model & Lines changed & Calls & Latency med. (s) & p90 (s) & Prompt tok. & Output tok. & Cost (USD) \\\\",
           "\\midrule"]
    last = None
    for b in buckets:
        model = b["model"].split("/")[1].split("@")[0]
        if last is not None and model != last:
            tex.append("\\midrule")
        md.append(f"| {model} | {b['bucket']} | {b['cases']} | {b['calls']} | {_n(b['latency_median_s'], '{:.1f}')} | "
                  f"{_n(b['latency_p90_s'], '{:.1f}')} | {_n(b['prompt_tokens_median'])} | "
                  f"{_n(b['completion_tokens_median'])} | {_n(b['cost_mean'], '{:.4f}')} |")
        tex.append(f"{model if model != last else ''} & {b['bucket'].replace('–', '--')} & {b['calls']} & "
                   f"{_n(b['latency_median_s'], '{:.1f}', '--')} & {_n(b['latency_p90_s'], '{:.1f}', '--')} & "
                   f"{_n(b['prompt_tokens_median'], '{:,.0f}', '--')} & {_n(b['completion_tokens_median'], '{:,.0f}', '--')} & "
                   f"{_n(b['cost_mean'], '{:.4f}', '--')} \\\\")
        last = model
    tex += ["\\bottomrule", "\\end{tabular}",
            "\\caption{Review latency and cost by the size of the change (lines added plus deleted). "
            "One call per row of the underlying runs; the contributor pays the cost.}",
            "\\label{tab:scaling}", "\\end{table}"]

    md += ["", "## What drives latency and cost", "",
           "Spearman rank correlation (1 = perfectly monotonic).", "",
           "| Model | Calls | lines→latency | lines→cost | lines→prompt tok | file bytes→prompt tok "
           "| prompt tok→latency | output tok→latency | s per +1k prompt tok | cost per 1k prompt tok (USD) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        model = s["model"].split("/")[1].split("@")[0]
        md.append(f"| {model} | {s['calls']} | {_n(s['rho_lines_latency'], '{:.2f}')} | "
                  f"{_n(s['rho_lines_cost'], '{:.2f}')} | {_n(s['rho_lines_prompt_tokens'], '{:.2f}')} | "
                  f"{_n(s['rho_filebytes_prompt_tokens'], '{:.2f}')} | {_n(s['rho_prompt_tokens_latency'], '{:.2f}')} | "
                  f"{_n(s['rho_completion_tokens_latency'], '{:.2f}')} | "
                  f"{_n(s['latency_s_per_1k_prompt_tokens'], '{:.2f}')} | {_n(s['cost_per_1k_prompt_tokens'], '{:.5f}')} |")
    return "\n".join(md) + "\n", "\n".join(tex) + "\n"


# ---------------------------------------------------------------- charts

def _style(ax, title, subtitle, xlabel, ylabel):
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    ax.set_xscale("log")
    ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.grid(axis="both", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK_MUTED)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    ax.set_xlabel(xlabel, fontsize=9, color=INK)
    ax.set_ylabel(ylabel, fontsize=9, color=INK)
    ax.set_title(title, loc="left", fontsize=10.5, color=INK, fontweight="bold", pad=14)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8, color=INK_MUTED)


def _per_case(joined, model, y):
    """Median of `y` per case (3 reps x templates -> one point per case)."""
    acc = defaultdict(list)
    lines = {}
    for r in joined:
        if r["model"] == model and r[y] is not None:
            acc[r["case"]].append(r[y])
            lines[r["case"]] = max(r["lines"], 1)
    return [(lines[c], statistics.median(v)) for c, v in acc.items()]


def _scatter_with_trend(ax, joined, y, scale=1.0):
    models = sorted({r["model"] for r in joined})
    for i, model in enumerate(models):
        pts = _per_case(joined, model, y)
        xs, ys = [p[0] for p in pts], [p[1] * scale for p in pts]
        name = model.split("/")[1].split("@")[0]
        ax.scatter(xs, ys, s=22, color=SERIES_COLORS[i], marker=MARKERS[i % 4], alpha=0.75,
                   edgecolors="white", linewidths=0.6, zorder=3, label=f"{name} (one point per case)")
        # Bucket medians as a step trend line.
        trend = []
        for lo, hi in BUCKETS:
            v = [yy for xx, yy in zip(xs, ys) if lo <= xx <= hi]
            if v:
                mid = math.sqrt(lo * min(hi, max(xs))) if hi != math.inf else max(lo, statistics.median(
                    [xx for xx in xs if xx >= lo]))
                trend.append((mid, statistics.median(v)))
        if len(trend) > 1:
            ax.plot([t[0] for t in trend], [t[1] for t in trend], color=SERIES_COLORS[i], linewidth=2, zorder=4,
                    label=f"{name} median by size bucket")
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", bbox_to_anchor=(1.0, 1.0))


def charts(joined: list[dict], out: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "pdf.fonttype": 42,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    written = []

    def save(fig, stem):
        for ext, kw in (("pdf", {}), ("png", {"dpi": 200})):
            p = out / f"{stem}.{ext}"
            fig.savefig(p, bbox_inches="tight", **kw)
            written.append(p)
        plt.close(fig)

    base = [r for r in joined if r["context"] in (None, "none")]

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    _scatter_with_trend(ax, base, "latency_ms", 1 / 1000)
    ax.set_ylim(bottom=0)
    _style(ax, "Review latency vs change size", "Median of the case's calls (2 templates × 3 runs).",
           "Lines changed (added + deleted, log scale)", "Latency per review (s)")
    save(fig, "latency_vs_size")

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    _scatter_with_trend(ax, base, "cost", 100)  # dollars -> cents
    ax.set_ylim(bottom=0)
    _style(ax, "Cost per review vs change size", "Paid by the contributor. Median of the case's calls.",
           "Lines changed (added + deleted, log scale)", "Cost per review (US cents)")
    save(fig, "cost_vs_size")

    fig, axes = plt.subplots(1, 2, figsize=(9.0, 3.3), sharey=True)
    models = sorted({r["model"] for r in base})
    for i, model in enumerate(models):
        name = model.split("/")[1].split("@")[0]
        per = defaultdict(list)
        meta = {}
        for r in base:
            if r["model"] == model and r["prompt_tokens"]:
                per[r["case"]].append(r["prompt_tokens"])
                meta[r["case"]] = (max(r["lines"], 1), max(r["changed_file_bytes"], 1))
        for ax, idx in ((axes[0], 0), (axes[1], 1)):
            ax.scatter([meta[c][idx] for c in per], [statistics.median(v) / 1000 for v in per.values()], s=20,
                       color=SERIES_COLORS[i], marker=MARKERS[i % 4], alpha=0.75, edgecolors="white",
                       linewidths=0.6, zorder=3, label=name)
    _style(axes[0], "Prompt size: diff vs whole files", "Each point is one case.",
           "Lines changed (log)", "Prompt tokens (thousands)")
    _style(axes[1], "", "", "Size of the changed files (bytes, log)", "")
    axes[1].legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    save(fig, "tokens_vs_size")
    return written


# ---------------------------------------------------------------- entry point

def write_all(rows: list[dict], cases_dir: Path, out: Path, work: Path, cache: Path, log=print) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    sizes_csv = out / "sizes.csv"
    sizes = load_or_measure(sizes_csv, cases_dir, {r["case"] for r in rows}, work, cache, log)
    fields = ["case", "files", "added", "deleted", "lines", "patch_bytes", "changed_file_bytes"]
    with sizes_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in sorted(sizes.values(), key=lambda s: s["case"]):
            w.writerow({k: s[k] for k in fields})

    joined = join(rows, sizes)
    with (out / "scaling.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(joined[0]))
        w.writeheader()
        w.writerows(joined)
    base = [r for r in joined if r["context"] in (None, "none")]
    md, tex = tables(bucket_table(base), model_summary(base))
    (out / "scaling.md").write_text(md, encoding="utf-8")
    (out / "scaling.tex").write_text(tex, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(model_summary(base), indent=2), encoding="utf-8")
    return [sizes_csv, out / "scaling.csv", out / "scaling.md", out / "scaling.tex", out / "summary.json"] + \
        charts(joined, out)
