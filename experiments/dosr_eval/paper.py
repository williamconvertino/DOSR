"""Paper outputs from a results file: LaTeX tables (booktabs) and PDF/PNG figures.

    tables.tex             \\input{} into the paper; needs \\usepackage{booktabs}
    fig_verdicts.{pdf,png} correct-verdict rate per case group, template A vs B, one panel per model
    fig_misleading.{pdf,png} reject rate on reverted fixes: neutral vs misleading PR text
    fig_framings.{pdf,png} bad PRs accepted per SEVRA framing (attacker economics), with 95% CIs
    framings.md            the framing table as Markdown, for quick reading

Figures need matplotlib (`pip install -e "experiments[figures]"`); tables do not.
Sized for a USENIX two-column page (3.33in column, 7in full width).
"""

from collections import defaultdict
from pathlib import Path

from .figures import wilson
from .metrics import GROUPS, case_group, group_order, summarize, verdict

# Validated categorical slots 1-3 (dataviz reference palette, light mode; the first three
# pass all-pairs colorblind checks); one per template.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
INK, INK_2, INK_3, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
COLUMN_IN, FULL_IN = 3.33, 7.0

TEMPLATE_LABELS = {"security-sensitive-review-v2": "A: verdict + reason",
                   "exp-evidence-v1": "B: evidence required",
                   "exp-blocking-only-v1": "C: block only regressions"}


def template_label(tid: str) -> str:
    return TEMPLATE_LABELS.get(tid, tid)


def _tm_key(tm: tuple) -> tuple:
    return (template_label(tm[0]), tm[1])


def _tex(s: str) -> str:
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("_", r"\_"), ("#", r"\#"),
                 ("$", r"\$"), ("{", r"\{"), ("}", r"\}")):
        s = s.replace(a, b)
    return s


def _pct(v) -> str:
    return "--" if v is None else f"{100 * v:.0f}\\%"


def _num(v, fmt="{:,.0f}") -> str:
    return "--" if v is None else fmt.format(v)


def attacker_cost(s: dict) -> float | None:
    """Expected spend to get one bad PR accepted: cost per review / miss rate."""
    if s["mean_cost"] is None or not s["miss"]:
        return None
    return s["mean_cost"] / s["miss"]


# ------------------------------------------------------------------ framings (attacker economics)

def framing(case_id: str) -> str | None:
    """How a SEVRA revert was presented: "neutral" for <slug>-revert, the axis for <slug>-revert-<axis>.

    None for anything else (fixes, SWE-bench cases). The last "-revert-" wins, so a slug that
    itself contains "revert" still parses.
    """
    if case_id.endswith("-revert"):
        return "neutral"
    _, sep, axis = case_id.rpartition("-revert-")
    return axis if sep and axis else None


def framing_rows(rows: list[dict]) -> list[dict]:
    """Scored SEVRA calls on should-reject cases that have a framing (errors are not scored)."""
    return [r for r in rows if r.get("dataset") == "SEVRA" and r["expected"] == "reject"
            and framing(r["case"]) is not None and verdict(r) is not None]


def framing_stats(rows: list[dict]) -> dict:
    """Accepted bad PRs among `rows` (one template, model and framing) and what one costs an attacker.

    attempts = 1 / accept rate and attacker_cost = mean cost / accept rate are None when nothing was
    accepted; rule_of_three (3/n, the 95% upper bound on the accept rate after 0 of n) is set only then.
    """
    n = len(rows)
    k = sum(verdict(r) == "approve" for r in rows)
    rate = k / n if n else None
    mean_cost = summarize(rows)["mean_cost"] if rows else None
    return {
        "n": n, "accepted": k, "rate": rate, "ci": wilson(k, n), "mean_cost": mean_cost,
        "attempts": 1 / rate if rate else None,
        "attacker_cost": mean_cost / rate if rate and mean_cost is not None else None,
        "rule_of_three": min(1.0, 3 / n) if n and not k else None,
    }


def framing_order(rows: list[dict]) -> list[str]:
    """Neutral first, then framings by pooled accept rate (most bad PRs accepted first), then name."""
    pooled = defaultdict(list)
    for r in framing_rows(rows):
        pooled[framing(r["case"])].append(r)
    rate = {f: framing_stats(rs)["rate"] for f, rs in pooled.items()}
    return sorted(rate, key=lambda f: (f != "neutral", -rate[f], f))


def framing_table(rows: list[dict]) -> list[tuple[str, str, str, dict]]:
    """(template, model, framing, stats) per template x model x framing that has calls, in display order."""
    by = defaultdict(list)
    for r in framing_rows(rows):
        by[(r["template"], r["model"], framing(r["case"]))].append(r)
    order = framing_order(rows)
    keys = sorted(by, key=lambda k: (*_tm_key(k[:2]), order.index(k[2])))
    return [(*k, framing_stats(by[k])) for k in keys]


def _framing_label(f: str) -> str:
    return "neutral (plain revert)" if f == "neutral" else f.replace("-", " ")


def framings_tex(rows: list[dict], currency: str = "") -> str:
    table = framing_table(rows)
    if not table:
        return ""
    cur = f" ({_tex(currency)})" if currency else ""
    out = [r"\begin{table*}[t]", r"\centering\small",
           r"\begin{tabular}{lllrrrrrr}", r"\toprule",
           r"Template & Model & Framing & Calls & Accepted [95\% CI] & Rule-of-3 bound & "
           rf"Cost/review{cur} & Attempts per accept & Cost per accepted bad PR{cur} \\", r"\midrule"]
    prev = None
    for tid, model, f, s in table:
        if prev and prev != (tid, model):
            out.append(r"\addlinespace")
        prev = (tid, model)
        lo, hi = s["ci"]
        bound = "--" if s["rule_of_three"] is None else r"$\le$ " + _pct(s["rule_of_three"])
        out.append(f"{_tex(template_label(tid))} & {_tex(model)} & {_tex(_framing_label(f))} & {s['n']} & "
                   f"{_pct(s['rate'])} [{100 * lo:.0f}--{100 * hi:.0f}] & {bound} & "
                   f"{_num(s['mean_cost'], '{:.4f}')} & {_num(s['attempts'], '{:.1f}')} & "
                   f"{_num(s['attacker_cost'], '{:.4f}')} \\\\")
    out += [r"\bottomrule", r"\end{tabular}",
            r"\caption{Reverted security fixes (SEVRA) accepted per PR framing: neutral is the plain revert, "
            r"the others carry SEVRA's misleading PR text for that social-engineering axis. Calls exclude "
            r"provider errors. Attempts per accept: 1 $\div$ accept rate; cost per accepted bad PR: "
            r"cost/review $\div$ accept rate, i.e.\ what an attacker expects to pay. With 0 accepted both are "
            r"undefined and the rule of three bounds the accept rate at 3/calls (95\%).}",
            r"\label{tab:framings}", r"\end{table*}", ""]
    return "\n".join(out)


def _md_num(v, fmt: str) -> str:
    return "—" if v is None else fmt.format(v)


def framings_md(rows: list[dict], currency: str = "") -> str:
    cur = f" ({currency})" if currency else ""
    lines = ["# Bad PRs accepted per framing (SEVRA reverts)", "",
             f"| Template | Model | Framing | Calls | Accepted | 95% CI | Rule-of-3 bound | Cost/review{cur} "
             f"| Attempts per accept | Cost per accepted bad PR{cur} |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for tid, model, f, s in framing_table(rows):
        lo, hi = s["ci"]
        bound = "—" if s["rule_of_three"] is None else f"≤ {s['rule_of_three']:.0%}"
        lines.append(f"| {template_label(tid)} | {model} | {_framing_label(f)} | {s['n']} | "
                     f"{s['accepted']} ({s['rate']:.0%}) | {lo:.0%}–{hi:.0%} | {bound} | "
                     f"{_md_num(s['mean_cost'], '{:.4f}')} | {_md_num(s['attempts'], '{:.1f}')} | "
                     f"{_md_num(s['attacker_cost'], '{:.4f}')} |")
    lines += ["", "Calls exclude provider errors. Attempts = 1 / accept rate; cost per accepted bad PR = "
              "cost/review / accept rate. With 0 accepted both are undefined; the rule-of-three bound "
              "(3 / calls) is the 95% upper limit on the accept rate."]
    return "\n".join(lines) + "\n"


def tables_tex(rows: list[dict], currency: str = "") -> str:
    by_tm = defaultdict(list)
    for r in rows:
        by_tm[(r["template"], r["model"])].append(r)
    cur = f" ({_tex(currency)})" if currency else ""

    out = [r"% Generated by `python -m dosr_eval figures`; do not edit by hand.",
           r"\begin{table*}[t]", r"\centering\small",
           r"\begin{tabular}{llrrrrrrrrr}", r"\toprule",
           r"Template & Model & Acc. & False rej. & Miss & Parse fail & Flip & Diagnosis & Prompt tok & "
           rf"Cost/review{cur} & Cost per accepted bad PR{cur} \\", r"\midrule"]
    for (tid, model), rs in sorted(by_tm.items(), key=lambda kv: _tm_key(kv[0])):
        s = summarize(rs)
        out.append(f"{_tex(template_label(tid))} & {_tex(model)} & {_pct(s['accuracy'])} & "
                   f"{_pct(s['false_reject'])} & {_pct(s['miss'])} & {_pct(s['parse_fail'])} & "
                   f"{_pct(s['flip'])} & {_pct(s['diagnosis'])} & {_num(s['mean_prompt_tokens'])} & "
                   f"{_num(s['mean_cost'], '{:.4f}')} & {_num(attacker_cost(s), '{:.2f}')} \\\\")
    out += [r"\bottomrule", r"\end{tabular}",
            r"\caption{Review quality and cost per template and model. False rej.: approve-labelled PRs "
            r"rejected; Miss: reject-labelled PRs approved; Flip: cases whose verdict changed across repeated runs; Diagnosis: correct rejections that name the "
            r"known problem (hand-graded); Cost per accepted bad PR: cost/review $\div$ miss rate.}",
            r"\label{tab:review-quality}", r"\end{table*}", ""]

    groups = sorted({case_group(r) for r in rows}, key=group_order)
    expected = {case_group(r): r["expected"] for r in rows}
    out += [r"\begin{table*}[t]", r"\centering\small",
            r"\begin{tabular}{ll" + "r" * len(groups) + "}", r"\toprule",
            "Template & Model & " + " & ".join(rf"\shortstack{{{_tex(g)}\\({expected[g]})}}" for g in groups)
            + r" \\", r"\midrule"]
    for (tid, model), rs in sorted(by_tm.items(), key=lambda kv: _tm_key(kv[0])):
        cells = []
        for g in groups:
            sub = [r for r in rs if case_group(r) == g]
            cells.append(_pct(summarize(sub)["accuracy"]) if sub else "--")
        out.append(f"{_tex(template_label(tid))} & {_tex(model)} & " + " & ".join(cells) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}",
            r"\caption{Share of correct verdicts per case group (expected verdict in parentheses).}",
            r"\label{tab:verdicts-by-group}", r"\end{table*}", ""]
    framings = framings_tex(rows, currency)
    return "\n".join(out) + ("\n" + framings if framings else "")


# ------------------------------------------------------------------ figures

def _style(plt) -> None:
    plt.rcParams.update({
        "font.size": 7, "axes.titlesize": 7.5, "axes.labelsize": 7, "xtick.labelsize": 6.5,
        "ytick.labelsize": 6.5, "legend.fontsize": 6.5, "axes.edgecolor": INK_3, "axes.labelcolor": INK_2,
        "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK, "axes.spines.top": False,
        "axes.spines.right": False, "pdf.fonttype": 42, "ps.fonttype": 42,
    })


def _save(fig, out: Path, name: str) -> list[Path]:
    paths = [out / f"{name}.pdf", out / f"{name}.png"]
    for p in paths:
        fig.savefig(p, dpi=300, bbox_inches="tight")
    return paths


def fig_verdicts(rows: list[dict], out: Path, plt) -> list[Path]:
    """Horizontal grouped bars: correct-verdict rate per case group, one bar per template."""
    models = sorted({r["model"] for r in rows})
    templates = sorted({r["template"] for r in rows}, key=template_label)[: len(SERIES)]
    groups = sorted({case_group(r) for r in rows}, key=group_order)
    expected = {case_group(r): r["expected"] for r in rows}
    width = COLUMN_IN if len(models) == 1 else FULL_IN
    fig, axes = plt.subplots(1, len(models), figsize=(width, 0.25 * len(groups) * len(templates) + 0.7),
                             sharey=True, squeeze=False)
    bar_h = 0.8 / len(templates)
    for ax, model in zip(axes[0], models):
        for i, tid in enumerate(templates):
            ys, xs = [], []
            for j, g in enumerate(groups):
                sub = [r for r in rows if r["model"] == model and r["template"] == tid and case_group(r) == g]
                acc = summarize(sub)["accuracy"] if sub else None
                y = j + (i - (len(templates) - 1) / 2) * bar_h
                ys.append(y)
                xs.append(acc)
                if acc is not None:
                    ax.text(100 * acc + 2, y, f"{100 * acc:.0f}%", va="center", fontsize=6, color=INK_2)
            ax.barh(ys, [100 * (x or 0) for x in xs], height=bar_h * 0.9, color=SERIES[i],
                    label=template_label(tid), edgecolor="white", linewidth=0.6)
        ax.set_xlim(0, 118)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.xaxis.grid(True, color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0)
        ax.set_xlabel("correct verdicts")
        if len(models) > 1:
            ax.set_title(model, loc="left", color=INK)
    axes[0][0].set_yticks(range(len(groups)))
    axes[0][0].set_yticklabels([f"{g}\n(should {expected[g]})" for g in groups])
    axes[0][0].invert_yaxis()
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(templates), frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout()
    paths = _save(fig, out, "fig_verdicts")
    plt.close(fig)
    return paths


def fig_misleading(rows: list[dict], out: Path, plt) -> list[Path] | None:
    """Dumbbell: reject rate on the same reverted fixes with neutral vs misleading PR text."""
    neutral_g, misleading_g = GROUPS[("SEVRA", "vuln")], GROUPS[("SEVRA", "deceptive")]
    pairs = []
    for (tid, model) in sorted({(r["template"], r["model"]) for r in rows}, key=_tm_key):
        rate = {}
        for g in (neutral_g, misleading_g):
            sub = [r for r in rows if r["template"] == tid and r["model"] == model and case_group(r) == g]
            rate[g] = summarize(sub)["accuracy"] if sub else None
        if None not in rate.values():
            pairs.append((f"{template_label(tid)}\n{model}", rate[neutral_g], rate[misleading_g]))
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 0.42 * len(pairs) + 0.8))
    for y, (_, a, b) in enumerate(pairs):
        ax.plot([100 * a, 100 * b], [y, y], color=INK_3, linewidth=1.2, zorder=1)
    ax.scatter([100 * a for _, a, _ in pairs], range(len(pairs)), s=28, facecolor="white", edgecolor=INK_2,
               linewidth=1.4, zorder=2, label="neutral message")
    ax.scatter([100 * b for _, _, b in pairs], range(len(pairs)), s=28, color=SERIES[1], edgecolor="white",
               linewidth=0.8, zorder=3, label="misleading PR text (SEVRA)")
    ax.set_yticks(range(len(pairs)))
    ax.set_yticklabels([p[0] for p in pairs], fontsize=6)
    ax.set_ylim(len(pairs) - 0.4, -0.6)  # top-to-bottom, with room above and below the dots
    ax.set_xlim(-6, 106)
    ax.set_xticks([0, 50, 100])
    ax.set_xticklabels(["0%", "50%", "100%"])
    ax.xaxis.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("reverted security fixes rejected")
    fig.legend(*ax.get_legend_handles_labels(), loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2,
               frameon=False)
    fig.tight_layout()
    paths = _save(fig, out, "fig_misleading")
    plt.close(fig)
    return paths


def fig_framings(rows: list[dict], out: Path, plt) -> list[Path] | None:
    """Horizontal grouped bars: bad PRs accepted per framing, one bar per template, 95% Wilson whiskers."""
    table = framing_table(rows)
    if not table:
        return None
    stats = {(tid, model, f): s for tid, model, f, s in table}
    models = sorted({k[1] for k in stats})
    templates = sorted({k[0] for k in stats}, key=template_label)[: len(SERIES)]
    framings = framing_order(rows)
    width = COLUMN_IN if len(models) == 1 else FULL_IN
    fig, axes = plt.subplots(1, len(models), figsize=(width, 0.25 * len(framings) * len(templates) + 0.7),
                             sharey=True, squeeze=False)
    bar_h = 0.8 / len(templates)
    for ax, model in zip(axes[0], models):
        for i, tid in enumerate(templates):
            ys, xs, err = [], [], [[], []]
            for j, f in enumerate(framings):
                s = stats.get((tid, model, f))
                y = j + (i - (len(templates) - 1) / 2) * bar_h
                ys.append(y)
                rate, (lo, hi) = (s["rate"], s["ci"]) if s else (0.0, (0.0, 0.0))
                xs.append(100 * rate)
                err[0].append(100 * (rate - lo))
                err[1].append(100 * (hi - rate))
                if s:  # label right of the whisker so text never sits on it
                    ax.text(100 * hi + 2, y, f"{100 * rate:.0f}%", va="center", fontsize=6, color=INK_2)
            ax.barh(ys, xs, height=bar_h * 0.9, color=SERIES[i], label=template_label(tid),
                    edgecolor="white", linewidth=0.6)
            ax.errorbar(xs, ys, xerr=err, fmt="none", ecolor=INK_2, elinewidth=0.6, capsize=1.5, capthick=0.6)
        ax.set_xlim(0, 118)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.xaxis.grid(True, color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0)
        ax.set_xlabel("reverted security fixes accepted (95% CI)")
        if len(models) > 1:
            ax.set_title(model, loc="left", color=INK)
    axes[0][0].set_yticks(range(len(framings)))
    axes[0][0].set_yticklabels([_framing_label(f).replace(" (", "\n(") for f in framings])
    axes[0][0].invert_yaxis()
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(templates), frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout()
    paths = _save(fig, out, "fig_framings")
    plt.close(fig)
    return paths


def write_outputs(rows: list[dict], out: Path, currency: str = "") -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "tables.tex"]
    written[0].write_text(tables_tex(rows, currency), encoding="utf-8")
    if framing_rows(rows):
        written.append(out / "framings.md")
        written[-1].write_text(framings_md(rows, currency), encoding="utf-8")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: wrote tables only (pip install -e \"experiments[figures]\")")
        return written
    _style(plt)
    written += fig_verdicts(rows, out, plt)
    written += fig_misleading(rows, out, plt) or []
    written += fig_framings(rows, out, plt) or []
    return written
