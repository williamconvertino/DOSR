"""`dosr` command-line interface.

Global options (before the subcommand):
    -C/--repo PATH        repository to operate on (default: search upward from cwd)
    --profile FILE        client profile JSON (default: $DOSR_PROFILE or git identity)
    --attestor-url URL    attestor base URL (default: $DOSR_ATTESTOR_URL, profile, or http://127.0.0.1:8080)
    --chain-state FILE    mock chain state file (default: $DOSR_CHAIN_STATE or ~/.dosr/mock-chain/state.json)
"""

import argparse
import json
import sys
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from dosr_chain import ChainError, MockChain
from dosr_git import GitError
from dosr_protocol.policy import PRESETS, PolicyError, build_policy, format_model, parse_model_spec
from dosr_protocol.pr_format import TEMPLATES, PRFormatError

from . import __version__
from .attestor import AttestorClient, AttestorError
from .config import ConfigError, DosrRepo, profiles_in_dir, resolve_attestor_url, resolve_profile
from .pipeline import BAD_REQUESTS, SubmitOptions, Submission
from .render import OUTCOME_STYLE, ms, progress_view, result_view, short, size
from .repo_ops import commit_all, create_repository, register_existing, repo_status

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------- helpers


def _ctx(args):
    profile = resolve_profile(args.profile)
    chain = MockChain(args.chain_state) if args.chain_state else MockChain()
    attestor = AttestorClient(resolve_attestor_url(args.attestor_url, profile))
    return profile, chain, attestor


def _repo(args) -> DosrRepo:
    return DosrRepo(args.repo) if args.repo else DosrRepo.find(".")


def _kv_table(rows) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column(overflow="fold")
    for k, v in rows:
        t.add_row(k, v if isinstance(v, Text) else Text(str(v)))
    return t


def _ok(flag: bool | None) -> Text:
    if flag is None:
        return Text("unknown", style="yellow")
    return Text("ok", style="green") if flag else Text("MISMATCH", style="bold red")


# ---------------------------------------------------------------- commands


def cmd_init(args):
    profile, chain, _ = _ctx(args)
    path = Path(args.path).resolve()
    name = args.name or path.name
    models = [parse_model_spec(m) for m in args.model] if args.model else None
    attestors = None
    if args.attestor:
        attestors = []
        for spec in args.attestor:
            key_id, _, address = spec.partition("=")
            attestors.append({"key_id": key_id, "scheme": "eip712-secp256k1", "address": address})
    if args.policy:
        policy = json.loads(Path(args.policy).read_text("utf-8"))
    else:
        policy = build_policy(
            args.preset,
            repo_name=name,
            approved_models=models,
            attestors=attestors,
            prompt_template=args.prompt_template,
            max_changed_files=args.max_changed_files,
            max_patch_bytes=args.max_patch_bytes,
            minimum_approvals=args.minimum_approvals,
        )
    with console.status("Creating repository (git init, genesis commit, chain registration)…"):
        t0 = time.perf_counter()
        created = create_repository(
            path,
            name=name,
            description=args.description or "",
            policy=policy,
            profile=profile,
            chain=chain,
            seed_dir=args.seed,
            register=not args.no_register,
        )
        took = (time.perf_counter() - t0) * 1000
    repo = created["repo"]
    console.print(
        Panel(
            _kv_table(
                [
                    ("Path", created["path"]),
                    ("Name", repo["name"]),
                    ("Repo ID", repo["repo_id"]),
                    ("Policy hash", repo["policy_hash"]),
                    ("Policy", f"{policy['policy_id']} · template {policy['review']['prompt_template']}"),
                    ("Models", ", ".join(format_model(m) for m in policy["review"]["approved_models"])),
                    ("Attestors", ", ".join(a["key_id"] for a in policy["attestors"])),
                    ("Limits", f"{policy['review']['max_changed_files']} files / {policy['review']['max_patch_bytes']} patch bytes"),
                    ("Genesis", created["genesis_git_oid"]),
                    ("Chain", f"registered (block {created['registration']['registered_block']})" if created["registration"] else "NOT registered (--no-register)"),
                    ("Created by", f"{profile.name} <{profile.email}>"),
                    ("Took", ms(took)),
                ]
            ),
            title="[bold green]DOSR repository created",
            border_style="green",
        )
    )
    console.print(f"Next: edit files in [bold]{created['path']}[/], then run [bold]dosr submit -m \"message\"[/].")


def cmd_status(args):
    profile, chain, attestor = _ctx(args)
    repo = _repo(args)
    st = repo_status(repo, chain)
    try:
        h = attestor.health()
        att = Text(f"up · protocol {h.get('protocol_version')} · {attestor.base_url}", style="green")
    except AttestorError as e:
        att = Text(f"DOWN · {e}", style="red")

    if st["in_sync"]:
        sync = Text("in sync with canonical HEAD", style="green")
    elif st["diverged"]:
        sync = Text("DIVERGED from canonical HEAD (rebase needed)", style="bold red")
    elif st["ahead"]:
        sync = Text(f"{st['ahead']} local commit(s) not yet accepted", style="yellow")
    else:
        sync = Text(st.get("chain_error") or "unknown", style="yellow")

    rows = [
        ("Repository", f"{st['name']}  ({st['path']})"),
        ("Repo ID", st["repo_id"]),
        ("Policy hash", Text.assemble(st["policy_hash"], "  ", _ok(st["policy_hash_ok"]))),
        ("Branch", st["branch"] or "(detached)"),
        ("Local HEAD", short(st["local_head"], 40)),
        ("Canonical HEAD", short(st["canonical_head"], 40) if st["registered"] else Text(st["chain_error"] or "—", style="red")),
        ("Sync", sync),
        ("Attestor", att),
        ("Client profile", f"{profile.name} <{profile.email}>"),
    ]
    console.print(Panel(_kv_table(rows), title="[bold]DOSR status", border_style="blue"))

    if st["changes"]:
        t = Table(title="Uncommitted changes", box=box.SIMPLE, title_justify="left")
        t.add_column("Status")
        t.add_column("Path")
        names = {"??": "untracked", " M": "modified", "M ": "staged", " D": "deleted", "A ": "added"}
        for c in st["changes"]:
            t.add_row(names.get(c["code"], c["code"]), c["path"])
        console.print(t)
    else:
        console.print("[dim]Working tree clean.[/]")


def cmd_config(args):
    _, chain, _ = _ctx(args)
    repo = _repo(args)
    policy = repo.policy
    cfg = repo.config
    onchain = chain.get_repository(cfg["repo_id"])
    rv = policy["review"]
    rows = [
        ("Name", cfg["name"]),
        ("Description", cfg.get("description") or "—"),
        ("Repo ID", cfg["repo_id"]),
        ("Chain / contract", f"{cfg['chain_id']} / {cfg['contract']}"),
        ("Policy ID", policy["policy_id"]),
        ("Policy hash (repo.json)", cfg["policy_hash"]),
        ("Policy hash (computed)", Text.assemble(repo.computed_policy_hash(), "  ", _ok(repo.computed_policy_hash() == cfg["policy_hash"]))),
        ("Policy hash (chain)", Text.assemble(onchain["policy_hash"], "  ", _ok(onchain["policy_hash"] == cfg["policy_hash"])) if onchain else Text("not registered", style="red")),
        ("Git object format", policy["git"]["object_format"]),
        ("Approved models", ", ".join(format_model(m) for m in rv["approved_models"])),
        ("Minimum approvals", rv["minimum_approvals"]),
        ("PR template", f"{rv['prompt_template']} — {TEMPLATES[rv['prompt_template']].description}" if rv["prompt_template"] in TEMPLATES else rv["prompt_template"]),
        ("Decision schema", rv["decision_schema"]),
        ("Limits", f"max {rv['max_changed_files']} changed files, {rv['max_patch_bytes']} patch bytes"),
        ("Attestors", "\n".join(f"{a['key_id']} {a['address']}" for a in policy["attestors"])),
        ("Storage", f"{policy['storage']['bundle_format']} via {policy['storage']['distribution']}"),
    ]
    console.print(Panel(_kv_table(rows), title="[bold]Repository configuration (immutable policy)", border_style="blue"))
    if args.raw:
        console.print(Syntax(json.dumps(policy, indent=2), "json"))


def cmd_diff(args):
    _, chain, _ = _ctx(args)
    repo = _repo(args)
    base = chain.get_head(repo.config["repo_id"]).partition(":")[2]
    text = repo.git.diff_worktree(base)
    if not text.strip():
        console.print("[dim]No changes relative to the canonical HEAD.[/]")
        return
    console.print(f"[dim]Changes relative to canonical HEAD {base[:12]} (committed + working tree):[/]")
    console.print(Syntax(text, "diff", word_wrap=True))


def cmd_commit(args):
    profile, _, _ = _ctx(args)
    repo = _repo(args)
    oid = commit_all(repo, args.message, profile)
    console.print(f"[green]✓[/] committed [bold]{oid[:12]}[/] as {profile.name}")


def cmd_submit(args):
    profile, chain, attestor = _ctx(args)
    repo = _repo(args)
    opts = SubmitOptions(
        message=args.message,
        model=parse_model_spec(args.model) if args.model else None,
        register=not args.no_register,
        dry_run=args.dry_run,
        bundle_mode=args.bundle_mode,
        bad_request=args.bad_request,
    )
    sub = Submission(repo, profile, attestor, chain, opts)
    title = f"[bold]DOSR submit[/] · {repo.name} · {profile.name} → {attestor.base_url}"

    if args.json:
        snap = sub.run()
    elif console.is_terminal:
        with Live(get_renderable=lambda: progress_view(sub.to_dict(), title), console=console,
                  refresh_per_second=15, transient=False):
            snap = sub.run()
    else:
        def log(s, _seen={}):
            for st in s.to_dict()["steps"]:
                if st["status"] in ("done", "failed") and _seen.get(st["id"]) != st["status"]:
                    _seen[st["id"]] = st["status"]
                    mark = "ok  " if st["status"] == "done" else "FAIL"
                    console.print(f"[{mark}] {st['label']:<28} {ms(st['duration_ms']):>9}  {st['error'] or st['detail']}")
        sub.add_listener(log)
        snap = sub.run()

    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        console.print(result_view(snap))
        if args.show_pr and (Path(snap["artifacts_dir"]) / "pr.md").exists():
            console.print(Markdown((Path(snap["artifacts_dir"]) / "pr.md").read_text("utf-8")))
    return {"approved": 0, "dry-run": 0, "rejected": 2}.get(snap["outcome"], 1)


def cmd_history(args):
    repo = _repo(args)
    runs = repo.list_submissions()[: args.limit]
    if not runs:
        console.print("[dim]No submissions yet.[/]")
        return
    t = Table(title=f"Submissions · {repo.name}", box=box.SIMPLE_HEAD, title_justify="left")
    for col in ("When", "Request ID", "Outcome", "Title", "Candidate", "PR words", "Sent / recv", "Total"):
        t.add_column(col, overflow="fold", no_wrap=col != "Title")
    for r in runs:
        label, style = OUTCOME_STYLE.get(r["outcome"], (r["outcome"], ""))
        t.add_row(
            time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("started_at") or 0)),
            r["request_id"],
            Text(label, style=style),
            (r.get("info", {}).get("pr") or {}).get("title", "—"),
            short(r.get("info", {}).get("candidate_git_oid")),
            f"{r['info']['pr_metrics']['total']['words']:,}" if r.get("info", {}).get("pr_metrics") else "—",
            _sent_recv(r.get("info", {}).get("transfer")),
            ms(r.get("elapsed_ms")),
        )
    console.print(t)


def _sent_recv(t: dict | None) -> str:
    if not t:
        return "—"
    sent = sum(t.get(k) or 0 for k in ("request_bytes", "policy_bytes", "bundle_bytes"))
    return f"{size(sent)} / {size(t.get('response_bytes'))}"


def cmd_show(args):
    repo = _repo(args)
    runs = repo.list_submissions()
    rid = args.request_id or (runs[0]["request_id"] if runs else None)
    if not rid:
        raise ConfigError("no submissions yet")
    data = repo.load_submission(rid)
    if args.json:
        print(json.dumps(data, indent=2))
        return
    console.print(result_view(data["run"]))
    if args.pr and "pr_markdown" in data:
        console.print(Markdown(data["pr_markdown"]))
    if args.response and "response" in data:
        console.print(Syntax(json.dumps(data["response"], indent=2), "json"))


def cmd_server(args):
    _, _, attestor = _ctx(args)
    t0 = time.perf_counter()
    health = attestor.health()
    took = (time.perf_counter() - t0) * 1000
    keys = attestor.keys()["keys"]
    rows = [("URL", attestor.base_url), ("Status", Text(health["status"], style="green")),
            ("Protocol", health.get("protocol_version")), ("Latency", ms(took))]
    rows += [(f"Key {i + 1}", f"{k['key_id']} · {k['scheme']} · {k['address']}") for i, k in enumerate(keys)]
    console.print(Panel(_kv_table(rows), title="[bold]Attestor", border_style="green"))


def cmd_register(args):
    _, chain, _ = _ctx(args)
    repo = _repo(args)
    reg = register_existing(repo, chain)
    console.print(f"[green]✓[/] registered {repo.name} with genesis {reg['genesis_git_oid']} (block {reg['registered_block']})")


def cmd_chain(args):
    _, chain, _ = _ctx(args)
    repos = chain.list_repositories()
    console.print(f"[dim]Mock chain state: {chain.state_path}[/]")
    if not repos:
        console.print("[dim]No repositories registered.[/]")
        return
    t = Table(box=box.SIMPLE_HEAD)
    for col in ("Name", "Repo ID", "Canonical HEAD", "Accepted", "Policy hash"):
        t.add_column(col, overflow="fold")
    for r in repos.values():
        t.add_row(r["name"], r["repo_id"][:18] + "…", short(r["canonical_head"]),
                  str(r["accepted_transition_count"]), r["policy_hash"][:18] + "…")
    console.print(t)


def cmd_profiles(args):
    profiles = profiles_in_dir(args.dir)
    if not profiles:
        console.print(f"[dim]No profiles in {args.dir}[/]")
        return
    t = Table(box=box.SIMPLE_HEAD)
    for col in ("File", "Name", "Email", "Review model", "Description"):
        t.add_column(col, overflow="fold")
    for p in profiles:
        t.add_row(p.id, p.name, p.email, format_model(p.review_model) if p.review_model else "(policy default)", p.description)
    console.print(t)


def cmd_gui(args):
    from .gui.server import serve

    serve(
        host=args.host,
        port=args.port,
        workspace=args.workspace,
        seeds_dir=args.seeds,
        profiles_dir=args.profiles,
        profile_path=args.profile,
        attestor_url=args.attestor_url,
        chain_state=args.chain_state,
        open_browser=not args.no_browser,
    )


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dosr", description="DOSR client: decentralized open-source review.")
    p.add_argument("--version", action="version", version=f"dosr {__version__}")
    p.add_argument("-C", "--repo", help="repository path (default: search upward from cwd)")
    p.add_argument("--profile", help="client profile JSON (default: $DOSR_PROFILE or git identity)")
    p.add_argument("--attestor-url", help="attestor base URL")
    p.add_argument("--chain-state", help="mock chain state file")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create a new DOSR repository (genesis + chain registration)")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--description", default="")
    s.add_argument("--preset", choices=sorted(PRESETS), default="standard", help="policy preset")
    s.add_argument("--policy", help="use this policy JSON file verbatim instead of a preset")
    s.add_argument("--seed", help="directory of initial files to copy in")
    s.add_argument("--model", action="append", help="approved model provider/model@version (repeatable)")
    s.add_argument("--attestor", action="append", help="trusted attestor KEY_ID=0xADDRESS (repeatable)")
    s.add_argument("--prompt-template", choices=sorted(TEMPLATES))
    s.add_argument("--max-changed-files", type=int)
    s.add_argument("--max-patch-bytes", type=int)
    s.add_argument("--minimum-approvals", type=int)
    s.add_argument("--no-register", action="store_true", help="do not register genesis on the chain")
    s.set_defaults(func=cmd_init)

    sub.add_parser("status", help="local vs canonical state, changes, attestor health").set_defaults(func=cmd_status)

    s = sub.add_parser("config", help="show the repository configuration and policy")
    s.add_argument("--raw", action="store_true", help="also print policy.json")
    s.set_defaults(func=cmd_config)

    sub.add_parser("diff", help="changes relative to the canonical HEAD").set_defaults(func=cmd_diff)

    s = sub.add_parser("commit", help="stage everything and commit as the current profile")
    s.add_argument("-m", "--message", required=True)
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("submit", help="commit (optional), bundle, build PR, request review, register")
    s.add_argument("-m", "--message", help="commit message for uncommitted changes")
    s.add_argument("--model", help="review model provider/model@version (default: profile, then policy)")
    s.add_argument("--dry-run", action="store_true", help="build everything but do not contact the attestor")
    s.add_argument("--no-register", action="store_true", help="do not submit an approval to the chain")
    s.add_argument("--bundle-mode", choices=["full", "thin"])
    s.add_argument("--bad-request", choices=sorted(BAD_REQUESTS), metavar="MODE",
                   help="DEMO: send a deliberately bad request so the attestor rejects it. MODE is one of: "
                        + "; ".join(f"{k} ({v})" for k, v in BAD_REQUESTS.items()))
    s.add_argument("--show-pr", action="store_true", help="print the generated PR afterwards")
    s.add_argument("--json", action="store_true", help="print the final result as JSON only")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("history", help="list past submissions")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("show", help="show a past submission (default: latest)")
    s.add_argument("request_id", nargs="?")
    s.add_argument("--pr", action="store_true", help="print the PR document")
    s.add_argument("--response", action="store_true", help="print the attestor response JSON")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    sub.add_parser("server", help="check attestor health and keys").set_defaults(func=cmd_server)
    sub.add_parser("register", help="register an existing repo's genesis on the chain").set_defaults(func=cmd_register)
    sub.add_parser("chain", help="show the mock chain state").set_defaults(func=cmd_chain)

    s = sub.add_parser("profiles", help="list client profiles in a directory")
    s.add_argument("--dir", default="demo/clients")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("gui", help="launch the local web GUI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--workspace", default=".", help="directory containing DOSR repositories")
    s.add_argument("--seeds", help="directory of seed projects offered when creating repos")
    s.add_argument("--profiles", help="directory of client profiles to switch between")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_gui)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (ConfigError, PolicyError, PRFormatError, ChainError, AttestorError, GitError, FileNotFoundError) as e:
        err.print(f"[bold red]error:[/] {e}")
        return 1
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
