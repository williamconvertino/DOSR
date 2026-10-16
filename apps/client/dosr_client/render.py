"""Rich renderables shared by CLI commands."""

import time

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .pipeline import fmt_bytes as size

ICONS = {
    "pending": ("·", "dim"),
    "running": ("▶", "bold yellow"),
    "done": ("✓", "bold green"),
    "failed": ("✗", "bold red"),
    "skipped": ("–", "dim"),
}

OUTCOME_STYLE = {
    "approved": ("APPROVED", "bold white on green"),
    "rejected": ("REJECTED", "bold white on red"),
    "blocked": ("BLOCKED BY CLIENT", "bold black on bright_yellow"),
    "error": ("ERROR", "bold white on dark_red"),
    "dry-run": ("DRY RUN", "bold black on cyan"),
    "running": ("RUNNING", "bold black on yellow"),
    "pending": ("PENDING", "dim"),
}


def ms(v) -> str:
    if v is None:
        return ""
    return f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.2f} s"


def transfer_text(t: dict | None) -> str:
    if not t:
        return ""
    sent = sum(t.get(k) or 0 for k in ("request_bytes", "policy_bytes", "bundle_bytes", "prompt_bytes"))
    return (
        f"sent {size(sent)} (request {size(t.get('request_bytes'))} · policy {size(t.get('policy_bytes'))} · "
        f"bundle {size(t.get('bundle_bytes'))} · prompt {size(t.get('prompt_bytes'))}) · "
        f"received {size(t.get('response_bytes'))}"
    )


def pr_size_text(m: dict | None) -> str:
    if not m:
        return ""
    t = m["total"]
    return f"{t['words']:,} words · {t['chars']:,} chars · {t['lines']:,} lines · ~{t['est_tokens']:,} tokens (est. chars/4)"


def short(oid: str | None, n: int = 12) -> str:
    if not oid:
        return "—"
    fmt, _, hexpart = oid.partition(":")
    return f"{fmt}:{hexpart[:n]}" if hexpart else oid[:n]


CHECK_MARK = {True: ("✓", "green"), False: ("✗", "bold red"), None: ("–", "dim")}


def checks_summary(snap: dict) -> str:
    info = snap.get("info", {})
    parts = []
    for label, key in (("client", "local_checks"), ("attestor", "attestor_checks")):
        cs = info.get(key)
        if cs:
            failed = sum(c["ok"] is False for c in cs)
            parts.append(f"{label}: {sum(c['ok'] is True for c in cs)}/{len(cs)} passed"
                         + (f", {failed} failed" if failed else ""))
    return " · ".join(parts)


def checks_table(snap: dict, full: bool = False) -> Table | None:
    """Local validation next to the attestor's own checks, by check name. Unless
    `full`, only the checks that failed somewhere are listed."""
    info = snap.get("info", {})
    local = {c["name"]: c for c in info.get("local_checks", [])}
    remote = {c["name"]: c for c in info.get("attestor_checks", [])}
    if not local and not remote:
        return None
    names = list(local) + [n for n in remote if n not in local]
    if not full:
        names = [n for n in names if (local.get(n) or {}).get("ok") is False or (remote.get(n) or {}).get("ok") is False]
        if not names:
            return None
    t = Table(title="Checks: client (before sending) vs attestor" + ("" if full else " — failures only"),
              box=box.SIMPLE, title_justify="left", expand=True)
    t.add_column("Check", no_wrap=True)
    t.add_column("Client", justify="center", no_wrap=True)
    t.add_column("Attestor", justify="center", no_wrap=True)
    t.add_column("Detail", overflow="fold", ratio=1)
    for name in names:
        lc, rc = local.get(name), remote.get(name)
        cells = []
        for c, sent in ((lc, True), (rc, bool(remote))):
            if c is None:
                cells.append(Text("·" if sent else "", style="dim"))
            else:
                mark, style = CHECK_MARK[c["ok"]]
                cells.append(Text(mark, style=style))
        detail = (rc and not rc["ok"] and rc.get("detail")) or (lc and lc["ok"] is False and lc.get("detail")) or ""
        failed = (lc and lc["ok"] is False) or (rc and rc["ok"] is False)
        t.add_row(Text(name, style="bold" if failed else ""), *cells, Text(detail, style="red" if failed else "dim"))
    return t


def steps_table(snap: dict) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("", width=2, no_wrap=True)
    t.add_column("Step", no_wrap=True)
    t.add_column("Time", justify="right", no_wrap=True, width=9)
    t.add_column("Detail", overflow="fold", ratio=1)
    for s in snap["steps"]:
        icon, style = ICONS[s["status"]]
        detail = Text(s["error"], style="red") if s["error"] else Text(s["detail"] or "", style="dim" if s["status"] in ("pending", "skipped") else "")
        if s["status"] == "pending" and not s["detail"]:
            detail = Text(f"est. {ms(s['estimate_ms'])}", style="dim")
        label_style = "bold" if s["status"] == "running" else ("dim" if s["status"] in ("pending", "skipped") else "")
        t.add_row(Text(icon, style=style), Text(s["label"], style=label_style), ms(s["duration_ms"]), detail)
    return t


def progress_view(snap: dict, title: str) -> Panel:
    elapsed, remaining = snap["elapsed_ms"], snap["remaining_ms"]
    total = max(elapsed + remaining, 1)
    header = Table.grid(expand=True)
    header.add_column()
    header.add_column(justify="right")
    running = next((s for s in snap["steps"] if s["status"] == "running"), None)
    now = Text(f"▶ {running['label']}…", style="yellow") if running else Text("")
    header.add_row(now, Text(f"elapsed {ms(elapsed)}  ·  remaining ~{ms(remaining)}", style="cyan"))
    bar = ProgressBar(total=total, completed=elapsed if not snap["finished_at"] else total)
    return Panel(Group(header, bar, steps_table(snap)), title=title, border_style="blue")


def result_view(snap: dict, full_checks: bool = False) -> Group:
    label, style = OUTCOME_STYLE.get(snap["outcome"], (snap["outcome"].upper(), "bold"))
    parts = []
    decision = snap.get("decision")
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold")
    body.add_column(overflow="fold")
    body.add_row("Decision", Text(f" {label} ", style=style))
    if decision:
        body.add_row("Summary", decision.get("summary", ""))
        if decision.get("issues") and not decision.get("approved"):
            body.add_row("Issues", "\n".join(f"• {i}" for i in decision["issues"]))
        body.add_row("Model", decision.get("model", ""))
        if decision.get("decided_by"):
            body.add_row("Decided by", decision["decided_by"])
        if decision.get("review_input_hash"):
            body.add_row("Signed hashes", f"prompt {decision['review_input_hash'][:18]}… · "
                                          f"model output {decision.get('response_hash', '')[:18]}…")
        body.add_row("Attestor", f"{decision.get('attestor_key_id')} ({decision.get('signer')})")
        if decision.get("expires_at"):
            body.add_row("Expires", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(decision["expires_at"])))
    info = snap.get("info", {})
    if info.get("parent_git_oid"):
        body.add_row("Transition", f"{short(info.get('parent_git_oid'))} → {short(info.get('candidate_git_oid'))}")
    if info.get("chain_event"):
        body.add_row("Chain", f"canonical HEAD is now {short(info['chain_event']['candidate_git_oid'])} "
                              f"(block {info['chain_event']['block']})")
    if info.get("pr_metrics"):
        body.add_row("PR size", pr_size_text(info["pr_metrics"]))
    if info.get("prompt"):
        pm = info["prompt"]
        body.add_row("Prompt", f"{pm['words']:,} words · ~{pm['est_tokens']:,} tokens · sha256 {pm['sha256'][:18]}…")
    if (info.get("pr") or {}).get("context_files"):
        body.add_row("Context files", ", ".join(info["pr"]["context_files"]))
    if info.get("forced"):
        body.add_row("Forced", Text("local validation was skipped (simulated modified client)", style="yellow"))
    if info.get("transfer"):
        body.add_row("Transfer", transfer_text(info["transfer"]))
    if info.get("bad_request"):
        body.add_row("Demo", Text(f"deliberately bad request: {info['bad_request']}", style="yellow"))
    if checks_summary(snap):
        body.add_row("Checks", checks_summary(snap))
    body.add_row("Total time", ms(snap["elapsed_ms"]))
    body.add_row("Request ID", snap["request_id"])
    body.add_row("Artifacts", snap["artifacts_dir"])
    border = {"approved": "green", "rejected": "red", "error": "red", "blocked": "yellow"}.get(snap["outcome"], "cyan")
    parts.append(Panel(body, title="Result", border_style=border))

    lat = Table(title="Latency by step", box=box.SIMPLE, title_justify="left", expand=True)
    lat.add_column("Step", no_wrap=True)
    lat.add_column("Time", justify="right", no_wrap=True)
    lat.add_column("", ratio=1)
    measured = [s for s in snap["steps"] if s["duration_ms"] is not None]
    longest = max((s["duration_ms"] for s in measured), default=1) or 1
    for s in measured:
        width = max(1, int(30 * s["duration_ms"] / longest))
        color = "red" if s["status"] == "failed" else "green"
        lat.add_row(s["label"], ms(s["duration_ms"]), Text("█" * width, style=color))
    if measured:
        parts.append(lat)
    checks = checks_table(snap, full_checks)
    if checks is not None:
        parts.append(checks)

    if snap["errors"]:
        parts.append(Panel("\n".join(f"• {e}" for e in snap["errors"]), title="Errors", border_style="red"))
    if snap["warnings"]:
        parts.append(Panel("\n".join(f"• {w}" for w in snap["warnings"]), title="Warnings", border_style="yellow"))
    return Group(*parts)
