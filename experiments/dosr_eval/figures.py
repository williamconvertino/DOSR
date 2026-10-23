"""Paper-ready tables and figures from results JSONL.

Outputs (in one directory):
  matrix.tex / matrix.md   accept and reject rate per template x matrix cell
                           (PR quality high/low x code good/bad), as in the plan sketch
  rates.tex / rates.md     accuracy, false reject, miss, parse fail, flip per template
  cost.tex / cost.md       tokens, cost and latency per review
  accept_by_cell.pdf/.png  accept rate per cell, one bar per template, 95% Wilson CIs
  rates.pdf/.png           the error rates as grouped bars, 95% Wilson CIs

Every number comes with its n: pilot samples are small, so the intervals are wide and
the figures say so. If only the mock reviewer was run, every output is marked as
placeholder data.

Style: light surface for print; series colors are the first slots of a validated
categorical palette (fixed order, never cycled), each also carrying a 45/135-degree
hatch so the figures survive grayscale printing; thin bars, recessive grid, one axis.
"""

import math
from collections import defaultdict
from pathlib import Path

from .metrics import summarize, verdict

CELLS = ("high-good", "high-bad", "low-good", "low-bad")
CELL_LABELS = {"high-good": "High quality, good code", "high-bad": "High quality, bad code",
               "low-good": "Low quality, good code", "low-bad": "Low quality, bad code"}
EXPECTED_ACCEPT = {"high-good": True, "high-bad": False, "low-good": False, "low-bad": False}

# Categorical slots 1-8 (light mode), fixed order.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
HATCHES = ["", "////", "\\\\\\\\", "xx", "..", "--", "++", "oo"]
INK, INK_MUTED, GRID = "#1f1f1e", "#5f5e58", "#e4e3dd"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def series_key(row: dict, multi_model: bool) -> str:
    return f"{row['template']} · {row['model']}" if multi_model else row["template"]


def is_placeholder(rows: list[dict]) -> bool:
    return all(r["model"].startswith("mock/") for r in rows)


def _series(rows):
    multi = len({r["model"] for r in rows}) > 1
    groups = defaultdict(list)
    for r in rows:
        groups[series_key(r, multi)].append(r)
    return dict(groups)  # insertion order = first appearance = stable color assignment


# ---------------------------------------------------------------- numbers

def matrix_data(rows: list[dict]) -> dict:
    """series -> cell -> {"n", "accept", "ci"} over scored calls."""
    out = {}
    for name, rs in _series(rows).items():
        out[name] = {}
        for cell in CELLS:
            scored = [r for r in rs if r.get("cell") == cell and verdict(r) is not None]
            k = sum(verdict(r) == "approve" for r in scored)
            out[name][cell] = {"n": len(scored), "accept": k / len(scored) if scored else None,
                               "ci": wilson(k, len(scored))}
    return out


def rate_data(rows: list[dict]) -> dict:
    """series -> metric -> {"value", "n", "ci"}."""
    out = {}
    for name, rs in _series(rows).items():
        scored = [r for r in rs if verdict(r) is not None]
        pos = [r for r in scored if r["expected"] == "approve"]
        neg = [r for r in scored if r["expected"] == "reject"]
        counts = {
            "accuracy": (sum(verdict(r) == r["expected"] for r in scored), len(scored)),
            "false_reject": (sum(verdict(r) == "reject" for r in pos), len(pos)),
            "miss": (sum(verdict(r) == "approve" for r in neg), len(neg)),
            "parse_fail": (sum(bool(r.get("parse_error")) for r in scored), len(scored)),
        }
        out[name] = {m: {"value": k / n if n else None, "n": n, "ci": wilson(k, n)} for m, (k, n) in counts.items()}
        out[name]["flip"] = {"value": summarize(rs)["flip"], "n": None, "ci": None}
    return out


# ---------------------------------------------------------------- tables

def _pct(v) -> str:
    return "--" if v is None else f"{100 * v:.0f}\\%"


def _pct_md(v) -> str:
    return "—" if v is None else f"{100 * v:.0f}%"


def _ci(ci, md=False) -> str:
    if ci is None:
        return ""
    lo, hi = ci
    return f" [{100 * lo:.0f}–{100 * hi:.0f}]" if md else f" [{100 * lo:.0f}--{100 * hi:.0f}]"


def _tex_escape(s: str) -> str:
    return s.replace("_", "\\_").replace("&", "\\&").replace("%", "\\%").replace("·", "/")


def _caption_note(placeholder: bool) -> str:
    return " \\textbf{Placeholder data (mock reviewer); not results.}" if placeholder else ""


def matrix_tables(rows: list[dict]) -> tuple[str, str]:
    data, ph = matrix_data(rows), is_placeholder(rows)
    tex = ["\\begin{table}[t]", "\\centering", "\\small",
           "\\begin{tabular}{ll" + "c" * len(CELLS) + "}", "\\toprule",
           " & & " + " & ".join(f"\\shortstack{{{CELL_LABELS[c].replace(', ', ',\\\\')}}}" for c in CELLS)
           + " \\\\", "\\midrule"]
    md = ["| Template | | " + " | ".join(CELL_LABELS[c] for c in CELLS) + " |",
          "|---|---|" + "---|" * len(CELLS)]
    for name, cells in data.items():
        acc, rej, acc_md, rej_md = [], [], [], []
        for c in CELLS:
            d = cells[c]
            a = d["accept"]
            want = EXPECTED_ACCEPT[c]
            a_txt, r_txt = _pct(a), _pct(None if a is None else 1 - a)
            # Bold the outcome the policy wants in this cell.
            if a is not None:
                a_txt, r_txt = (f"\\textbf{{{a_txt}}}", r_txt) if want else (a_txt, f"\\textbf{{{r_txt}}}")
            acc.append(f"{a_txt}{_ci(d['ci'])}")
            rej.append(r_txt + (f" (n={d['n']})" if d["n"] else ""))
            acc_md.append(f"{_pct_md(a)}{_ci(d['ci'], md=True)}" + (" ✓" if want and a is not None else ""))
            rej_md.append(f"{_pct_md(None if a is None else 1 - a)}" + (" ✓" if not want and a is not None else "")
                          + (f" (n={d['n']})" if d["n"] else ""))
        tex.append(f"\\multirow{{2}}{{*}}{{{_tex_escape(name)}}} & Accept & " + " & ".join(acc) + " \\\\")
        tex.append(" & Reject & " + " & ".join(rej) + " \\\\")
        tex.append("\\midrule")
        md.append(f"| {name} | Accept | " + " | ".join(acc_md) + " |")
        md.append("| | Reject | " + " | ".join(rej_md) + " |")
    tex[-1] = "\\bottomrule"
    tex += ["\\end{tabular}",
            "\\caption{Accept and reject rate of each review template by PR quality and code correctness. "
            "Bold: the outcome the policy requires (only high-quality PRs with good code are accepted). "
            "Brackets: 95\\% Wilson interval on the accept rate." + _caption_note(ph) + "}",
            "\\label{tab:template-matrix}", "\\end{table}"]
    md += ["", "✓ marks the outcome the policy requires. Brackets: 95% Wilson interval on the accept rate."
           + (" **Placeholder data (mock reviewer); not results.**" if ph else "")]
    return "\n".join(tex) + "\n", "\n".join(md) + "\n"


RATE_LABELS = {"accuracy": "Accuracy", "false_reject": "False reject", "miss": "Miss",
               "parse_fail": "Parse fail", "flip": "Flip"}


def rate_tables(rows: list[dict]) -> tuple[str, str]:
    data, ph = rate_data(rows), is_placeholder(rows)
    metrics = list(RATE_LABELS)
    tex = ["\\begin{table}[t]", "\\centering", "\\small", "\\begin{tabular}{l" + "c" * len(metrics) + "}",
           "\\toprule", "Template & " + " & ".join(RATE_LABELS[m] for m in metrics) + " \\\\", "\\midrule"]
    md = ["| Template | " + " | ".join(RATE_LABELS[m] for m in metrics) + " |", "|---|" + "---|" * len(metrics)]
    for name, d in data.items():
        tex.append(_tex_escape(name) + " & " + " & ".join(_pct(d[m]["value"]) + _ci(d[m]["ci"]) for m in metrics)
                   + " \\\\")
        md.append(f"| {name} | " + " | ".join(_pct_md(d[m]["value"]) + _ci(d[m]["ci"], md=True) for m in metrics)
                  + " |")
    tex += ["\\bottomrule", "\\end{tabular}",
            "\\caption{Review error rates per template. False reject: share of should-accept reviews that "
            "rejected; miss: share of should-reject reviews that accepted; parse fail: output the attestor "
            "cannot parse (counted as a rejection); flip: cases whose verdict changed across repetitions. "
            "Brackets: 95\\% Wilson interval." + _caption_note(ph) + "}",
            "\\label{tab:template-rates}", "\\end{table}"]
    return "\n".join(tex) + "\n", "\n".join(md) + "\n"


def cost_tables(rows: list[dict], currency: str = "") -> tuple[str, str]:
    ph = is_placeholder(rows)
    tex = ["\\begin{table}[t]", "\\centering", "\\small", "\\begin{tabular}{lrrrr}", "\\toprule",
           f"Template & Prompt tokens & Output tokens & Cost / review{(' (' + currency + ')') if currency else ''}"
           " & Latency (s) \\\\", "\\midrule"]
    md = ["| Template | Prompt tokens | Output tokens | Cost / review | Latency (s) |", "|---|---|---|---|---|"]
    for name, rs in _series(rows).items():
        s = summarize(rs)
        scored = [r for r in rs if verdict(r) is not None]
        costs = [r["cost"] for r in scored if r.get("cost") is not None]
        per = sum(costs) / len(costs) if costs else None
        cells = [
            "--" if s["mean_prompt_tokens"] is None else f"{s['mean_prompt_tokens']:,.0f}",
            "--" if s["mean_completion_tokens"] is None else f"{s['mean_completion_tokens']:,.0f}",
            "--" if per is None else f"{per:.5f}",
            "--" if s["mean_latency_ms"] is None else f"{s['mean_latency_ms'] / 1000:.1f}",
        ]
        tex.append(_tex_escape(name) + " & " + " & ".join(cells) + " \\\\")
        md.append(f"| {name} | " + " | ".join(c.replace("--", "—") for c in cells) + " |")
    tex += ["\\bottomrule", "\\end{tabular}",
            "\\caption{Mean tokens, cost, and latency of one review, paid by the contributor."
            + _caption_note(ph) + "}", "\\label{tab:template-cost}", "\\end{table}"]
    return "\n".join(tex) + "\n", "\n".join(md) + "\n"


# ---------------------------------------------------------------- charts

def _grouped_bars(ax, groups: list[str], series: dict[str, list], title: str, subtitle: str):
    """series: name -> list of (value|None, ci|None) per group."""
    n = len(series)
    width = min(0.8 / max(n, 1), 0.28)
    for i, (name, vals) in enumerate(series.items()):
        xs = [g + (i - (n - 1) / 2) * width for g in range(len(groups))]
        heights = [0 if v is None else v for v, _ in vals]
        bars = ax.bar(xs, heights, width * 0.9, label=name, color=SERIES_COLORS[i % 8],
                      hatch=HATCHES[i % 8], edgecolor="white", linewidth=0.8, zorder=2)
        for b in bars:
            b.set_edgecolor("white")
        # max(0, ...): at p = 0 or 1 the interval bound can land a rounding error past the bar.
        err_lo = [0 if (v is None or ci is None) else max(0.0, v - ci[0]) for v, ci in vals]
        err_hi = [0 if (v is None or ci is None) else max(0.0, ci[1] - v) for v, ci in vals]
        ax.errorbar(xs, heights, yerr=[err_lo, err_hi], fmt="none", ecolor=INK_MUTED, elinewidth=1,
                    capsize=2.5, zorder=3)
        for x, (v, _) in zip(xs, vals):
            if v is None:
                ax.text(x, 0.01, "n/a", ha="center", va="bottom", fontsize=7, color=INK_MUTED, rotation=90)
            elif v == 0:  # a zero-height bar is invisible; say it is zero
                ax.text(x, 0.01, "0%", ha="center", va="bottom", fontsize=7, color=INK_MUTED)
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels(groups, fontsize=8.5, color=INK)
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.tick_params(axis="y", labelsize=8, colors=INK_MUTED, length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(INK_MUTED)
    ax.set_title(title, loc="left", fontsize=10.5, color=INK, fontweight="bold", pad=14)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8, color=INK_MUTED)
    if n > 1:
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.0))


def _save(fig, out: Path, stem: str) -> list[Path]:
    paths = [out / f"{stem}.pdf", out / f"{stem}.png"]
    fig.savefig(paths[0], bbox_inches="tight")
    fig.savefig(paths[1], bbox_inches="tight", dpi=200)
    return paths


def charts(rows: list[dict], out: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "pdf.fonttype": 42, "hatch.linewidth": 0.6,
                         "hatch.color": "white", "figure.facecolor": "white", "axes.facecolor": "white"})
    note = "Placeholder data (mock reviewer); not results. " if is_placeholder(rows) else ""
    written = []

    md = matrix_data(rows)
    if any(md[s][c]["n"] for s in md for c in CELLS):
        fig, ax = plt.subplots(figsize=(7.0, 3.0))
        groups = [CELL_LABELS[c].replace(", ", ",\n") + ("\n(should accept)" if EXPECTED_ACCEPT[c] else
                                                         "\n(should reject)") for c in CELLS]
        series = {s: [(md[s][c]["accept"], md[s][c]["ci"]) for c in CELLS] for s in md}
        _grouped_bars(ax, groups, series, "Accept rate by PR quality and code correctness",
                      note + "Error bars: 95% Wilson interval.")
        written += _save(fig, out, "accept_by_cell")
        plt.close(fig)

    rd = rate_data(rows)
    metrics = ["false_reject", "miss", "parse_fail"]
    fig, ax = plt.subplots(figsize=(6.0, 2.8))
    series = {s: [(rd[s][m]["value"], rd[s][m]["ci"]) for m in metrics] for s in rd}
    _grouped_bars(ax, [RATE_LABELS[m] for m in metrics], series, "Review error rates (lower is better)",
                  note + "Error bars: 95% Wilson interval.")
    written += _save(fig, out, "rates")
    plt.close(fig)
    return written


def write_all(rows: list[dict], out: Path, currency: str = "") -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, (tex, md) in {"matrix": matrix_tables(rows), "rates": rate_tables(rows),
                            "cost": cost_tables(rows, currency)}.items():
        (out / f"{stem}.tex").write_text(tex, encoding="utf-8")
        (out / f"{stem}.md").write_text(md, encoding="utf-8")
        written += [out / f"{stem}.tex", out / f"{stem}.md"]
    return written + charts(rows, out)
