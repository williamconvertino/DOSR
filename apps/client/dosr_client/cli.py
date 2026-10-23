"""`dosr` command-line interface.

Global options (before the subcommand):
    -C/--repo PATH        repository to operate on (default: search upward from cwd)
    --profile FILE        client profile JSON (default: $DOSR_PROFILE or git identity)
    --attestor-url URL    attestor base URL (default: $DOSR_ATTESTOR_URL, profile, or http://127.0.0.1:8080)
    --chain-state FILE    mock chain state file (default: $DOSR_CHAIN_STATE or ~/.dosr/mock-chain/state.json)
    --ipfs-api URL        IPFS (Kubo) RPC API (default: $DOSR_IPFS_API or http://127.0.0.1:5001)
    --p2p MODE            auto (IPFS if running, else local store) | ipfs | local | off
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
from dosr_protocol.policy import PRESETS, PolicyError, build_policy, format_model
from dosr_protocol.pr_format import TEMPLATES, PRFormatError

from . import __version__
from .attestor import AttestorClient, AttestorError
from .config import (
    ConfigError,
    DosrRepo,
    profiles_in_dir,
    provider_api_key,
    provider_key_env,
    resolve_attestor_url,
    resolve_profile,
)
from .templates import (
    TemplateError,
    available_templates,
    load_template_file,
    policy_template,
    preview,
    resolve_template,
    scaffold,
    template_dirs,
    to_toml,
)
from .inbox import normalize_url, send_announcement
from .ipfs import IpfsStore, LocalStore, P2PStore, StoreError
from .p2p import P2PError, import_share, publish
from .pipeline import BAD_REQUESTS, SubmitOptions, Submission, approved_model_spec, model_choices, parse_model_choice
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


def _store(args) -> P2PStore | None:
    if args.p2p == "off":
        return None
    return P2PStore(IpfsStore(args.ipfs_api), LocalStore(args.local_store), mode=args.p2p)


def _publisher(args, profile, chain):
    store = _store(args)
    if store is None:
        return None
    return lambda repo: publish(repo, chain, store, profile)


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
    models = [approved_model_spec(m) for m in args.model] if args.model else None
    attestors = None
    if args.attestor:
        attestors = []
        for spec in args.attestor:
            key_id, _, address = spec.partition("=")
            attestors.append({"key_id": key_id, "scheme": "eip712-secp256k1", "address": address})
    template_id, template_def = None, None
    if args.template:
        template, template_def = resolve_template(args.template)
        template_id = template.id
    if args.policy:
        policy = json.loads(Path(args.policy).read_text("utf-8"))
    else:
        policy = build_policy(
            args.preset,
            repo_name=name,
            approved_models=models,
            attestors=attestors,
            prompt_template=template_id,
            template=template_def,
            max_changed_files=args.max_changed_files,
            max_patch_bytes=args.max_patch_bytes,
            minimum_approvals=args.minimum_approvals,
            context_files=args.context_file,
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
                    ("Policy", f"{policy['policy_id']} · template {policy['review']['prompt_template']}"
                               + (" (custom, embedded in the policy)" if "template" in policy["review"] else "")),
                    ("Models", ", ".join(format_model(m) for m in policy["review"]["approved_models"])),
                    ("Context files", ", ".join(policy["review"].get("context_files", [])) or "(template default)"),
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
        ("Extra context files", ", ".join(rv.get("context_files", [])) or "—"),
        ("Minimum approvals", rv["minimum_approvals"]),
        ("PR template", _template_summary(policy)),
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
        model=parse_model_choice(args.model) if args.model else None,
        register=not args.no_register,
        dry_run=args.dry_run,
        bundle_mode=args.bundle_mode,
        bad_request=args.bad_request,
        force=args.force,
        publish=not args.no_publish,
    )
    sub = Submission(repo, profile, attestor, chain, opts, _publisher(args, profile, chain))
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
        console.print(result_view(snap, full_checks=args.show_checks))
        if args.show_pr and (Path(snap["artifacts_dir"]) / "pr.md").exists():
            console.print(Markdown((Path(snap["artifacts_dir"]) / "pr.md").read_text("utf-8")))
        if args.show_prompt and (Path(snap["artifacts_dir"]) / "prompt.txt").exists():
            console.print(Panel((Path(snap["artifacts_dir"]) / "prompt.txt").read_text("utf-8"),
                                title="Prompt sent to the attestor", border_style="dim"))
    return {"approved": 0, "dry-run": 0, "rejected": 2, "blocked": 3}.get(snap["outcome"], 1)


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
    sent = sum(t.get(k) or 0 for k in ("request_bytes", "policy_bytes", "bundle_bytes", "prompt_bytes"))
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
    console.print(result_view(data["run"], full_checks=True))
    if args.pr and "pr_markdown" in data:
        console.print(Markdown(data["pr_markdown"]))
    if args.prompt and "prompt" in data:
        console.print(Panel(data["prompt"], title="Prompt sent to the attestor", border_style="dim"))
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


def _template_summary(policy) -> str:
    try:
        t = policy_template(policy)
    except Exception as e:
        return f"{policy['review']['prompt_template']} (invalid: {e})"
    kind = "built-in" if t.builtin else "custom, embedded in the policy"
    return f"{t.id} ({kind}) — {t.description}\nhash {t.hash}"


def _template_table(t) -> Table:
    sections = " → ".join(s["type"] + (f" “{s['heading']}”" if s.get("heading") else "") for s in t.sections)
    return _kv_table([
        ("ID", t.id),
        ("Kind", "built-in" if t.builtin else "custom"),
        ("Description", t.description),
        ("Hash", t.hash),
        ("Diff context", f"{t.diff_context_lines} lines"),
        ("Full changed files", "yes" if t.include_full_files else "no"),
        ("Context files", ", ".join(t.context_globs) or "—"),
        ("Max file bytes", f"{t.max_file_bytes:,}"),
        ("Instructions", t.instructions),
        ("Checklist", "\n".join(f"• {c}" for c in t.checklist) or "—"),
        ("Layout", sections),
    ])


def _load_template_arg(spec: str | None, args):
    """A template id/file, or (no spec) the current repository's template."""
    if spec:
        if spec in TEMPLATES:
            return TEMPLATES[spec]
        return resolve_template(spec)[0]
    return policy_template(_repo(args).policy)


def cmd_template(args):
    if args.template_cmd == "list":
        t = Table(box=box.SIMPLE_HEAD)
        for col in ("ID", "Source", "Description"):
            t.add_column(col, overflow="fold")
        for row in available_templates():
            if row.get("error"):
                t.add_row(f"[red]{row['id']}[/]", row["path"], f"[red]{row['error']}[/]")
            else:
                t.add_row(row["id"], row["path"] or "built-in", row["description"])
        console.print(t)
        console.print("[dim]Searched: " + ", ".join(str(d) for d in template_dirs()) +
                      " (set DOSR_TEMPLATES to add directories).[/]")
    elif args.template_cmd == "show":
        t = _load_template_arg(args.template, args)
        if args.toml:
            console.print(to_toml(t), markup=False, highlight=False)
        else:
            console.print(Panel(_template_table(t), title=f"[bold]PR template {t.id}"))
        if args.preview:
            console.print(Panel(Syntax(preview(t), "markdown", word_wrap=True), title="Preview on a sample change (the exact PR text the reviewer gets)"))
    elif args.template_cmd == "new":
        base = _load_template_arg(args.base, args) if args.base else TEMPLATES["standard-code-review-v1"]
        out = Path(args.output or f"{args.id}.toml")
        if out.exists() and not args.overwrite:
            raise TemplateError(f"{out} already exists (use --overwrite)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(scaffold(args.id, base, args.description), encoding="utf-8")
        console.print(f"[green]✓[/] wrote [bold]{out}[/] (copied from {base.id}). Edit it, then:\n"
                      f"  dosr template check {out} --preview\n  dosr init PATH --template {out}")
    elif args.template_cmd == "check":
        t = load_template_file(args.file)
        if t.id in TEMPLATES:
            raise TemplateError(f"id {t.id!r} is a built-in template; give your template its own id")
        console.print(f"[green]✓[/] {args.file}: valid template [bold]{t.id}[/] (hash {t.hash})")
        console.print(Panel(_template_table(t), title=f"[bold]PR template {t.id}"))
        if args.preview:
            console.print(Panel(Syntax(preview(t), "markdown", word_wrap=True), title="Preview on a sample change (the exact PR text the reviewer gets)"))


def cmd_models(args):
    """Common review models (OpenAI, Claude, Gemini, GLM, mock) and, inside a DOSR repo,
    which of them its policy approves."""
    from dosr_protocol.models import catalog_choices

    policy = None
    try:
        policy = _repo(args).policy
    except Exception:
        pass
    rows = model_choices(policy) if policy else [dict(c, approved=None, request=c["spec"]) for c in catalog_choices()]
    if args.provider:
        from dosr_protocol.models import canonical_provider
        rows = [r for r in rows if r["provider"] == canonical_provider(args.provider)]
    t = Table(box=box.SIMPLE_HEAD)
    for col in ("Model (use with --model)", "Name", "API model id", "Key") + (("Policy",) if policy else ()):
        t.add_column(col, overflow="fold")
    for r in rows:
        env = provider_key_env(r["provider"])
        key = "[dim]none needed[/]" if not env else ("[green]✓[/] " if provider_api_key(r["provider"]) else "[red]✗[/] ") + env
        cells = [r["request"], f"{r['provider_label']} · {r['label']}", r["api_model"], key]
        if policy:
            cells.append("[green]approved[/]" + (f" [dim]({r['spec']})[/]" if r.get("range") else "")
                         if r["approved"] else "[dim]not approved[/]")
        t.add_row(*cells)
    console.print(t)
    console.print("[dim]Any other provider/model@version can be typed by hand. With --model, the version may be "
                  "omitted (openai/gpt-4o): it then comes from the policy, else the newest snapshot above. "
                  "Aliases: claude → anthropic, google → gemini.[/]")


def cmd_publish(args):
    profile, chain, _ = _ctx(args)
    repo = _repo(args)
    store = _store(args) or P2PStore(mode="auto")
    with console.status("Publishing the canonical HEAD (git bundle + signed history)…"):
        pub = publish(repo, chain, store, profile)
    rows = [
        ("Share code", Text(pub["share"], style="bold green")),
        ("Stored in", "IPFS" if pub["backend"] == "ipfs" else "local store (IPFS not running: works on this computer only)"),
        ("Canonical HEAD", pub["canonical_head"]),
        ("Accepted PRs", str(pub["accepted_transitions"])),
        ("Bundle", f"{pub['bundle']['id']} · {size(pub['bundle']['size_bytes'])}"),
        ("Took", ms(pub["took_ms"])),
    ]
    console.print(Panel(_kv_table(rows), title=f"[bold green]Published {repo.name}", border_style="green"))
    console.print(f"On the other device: [bold]dosr import {pub['share']}[/]")
    for url in args.announce or []:
        send_announcement(url, {"share": pub["share"], "repo_id": repo.config["repo_id"], "name": repo.name,
                                "canonical_head": pub["canonical_head"],
                                "accepted_transitions": pub["accepted_transitions"],
                                "from_name": f"{profile.name} <{profile.email}>"})
        console.print(f"[green]✓[/] announced to {normalize_url(url)}")


def cmd_import(args):
    _, chain, _ = _ctx(args)
    store = _store(args) or P2PStore(mode="auto")
    if args.workspace:
        workspace = Path(args.workspace)
    elif args.command == "pull":
        workspace = _repo(args).path.parent
    else:
        workspace = Path(".")
    with console.status("Fetching the share manifest and bundle…"):
        res = import_share(workspace, chain, store, args.share, expect_head=args.expect_head, timeout=args.timeout,
                           replace=args.adopt)
    title = {"clone": "Cloned", "pull": "Pulled"}[res["action"]]
    head = res["canonical_head"]
    if res["previous_head"] and res["previous_head"] != head:
        head += f"  (was {short(res['previous_head'])})"
    rows = [
        ("Repository", f"{res['name']}  ({res['path']})"),
        ("Status", res["status"]),
        ("Canonical HEAD", head),
        ("Chain", ("genesis registered (trusted from the share code); " if res["registered"] else "")
                  + f"{len(res['applied'])} signed transition(s) verified and applied"),
        ("Working copy", res["working_copy"]),
        ("Published by", (res["published_by"] or "?") + (f" on {res['device']}" if res.get("device") else "")),
        ("Transferred", f"{size(res['bundle_bytes'])} bundle · {ms(res['took_ms'])}"),
    ]
    for a in res["applied"]:
        rows.append((f"  block {a['block']}", f"{short(a['parent'])} → {short(a['candidate'])} · {a['model']}"))
    console.print(Panel(_kv_table(rows), title=f"[bold green]{title} {res['name']}", border_style="green"))
    for w in res["warnings"]:
        console.print(f"[yellow]warning:[/] {w}")


def cmd_p2p(args):
    store = _store(args) or P2PStore(mode="auto")
    st = store.status()
    rows = [("Mode", st["mode"]), ("Backend", st["backend"]), ("IPFS API", st["ipfs_api"]),
            ("Local store", st["local_store"])]
    if st.get("ipfs"):
        i = st["ipfs"]
        rows += [("Peer ID", i["peer_id"]), ("Node", i.get("agent") or "?"), ("Connected peers", str(i["peers"])),
                 ("Addresses", "\n".join(i["shareable_addresses"]) or "—")]
    elif st.get("ipfs_error"):
        rows.append(("IPFS", Text(st["ipfs_error"], style="yellow")))
    console.print(Panel(_kv_table(rows), title="[bold]P2P storage", border_style="blue"))
    if args.connect:
        for r in store.ipfs.connect(args.connect):
            console.print(("[green]✓[/] " if r["ok"] else "[red]✗[/] ") + r["addr"] + ("" if r["ok"] else f"  {r['error']}"))


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
        env_file=args.env_file,
        ipfs_api=args.ipfs_api,
        local_store=args.local_store,
        p2p_mode=args.p2p,
        p2p_port=args.p2p_port,
        p2p_host=args.p2p_host,
        device=args.device,
    )


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dosr", description="DOSR client: decentralized open-source review.")
    p.add_argument("--version", action="version", version=f"dosr {__version__}")
    p.add_argument("-C", "--repo", help="repository path (default: search upward from cwd)")
    p.add_argument("--profile", help="client profile JSON (default: $DOSR_PROFILE or git identity)")
    p.add_argument("--attestor-url", help="attestor base URL")
    p.add_argument("--chain-state", help="mock chain state file")
    p.add_argument("--ipfs-api", help="IPFS (Kubo) RPC API URL (default: $DOSR_IPFS_API or http://127.0.0.1:5001)")
    p.add_argument("--local-store", help="fallback content store directory when IPFS is not running "
                                         "(default: $DOSR_LOCAL_STORE or ~/.dosr/local-store)")
    p.add_argument("--p2p", choices=["auto", "ipfs", "local", "off"], default="auto",
                   help="where to publish repositories: auto = IPFS if the node is running, else the local store")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create a new DOSR repository (genesis + chain registration)")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--description", default="")
    s.add_argument("--preset", choices=sorted(PRESETS), default="standard", help="policy preset")
    s.add_argument("--policy", help="use this policy JSON file verbatim instead of a preset")
    s.add_argument("--seed", help="directory of initial files to copy in")
    s.add_argument("--model", action="append",
                   help="approved model provider/model@version, e.g. openai/gpt-4o@>=2024-08-06, "
                        "anthropic/claude-sonnet-4-5@20250929, gemini/gemini-2.5-pro@stable; version may be a "
                        "range like ^1.7.1; omitted = newest catalog snapshot (repeatable; see `dosr models`)")
    s.add_argument("--attestor", action="append", help="trusted attestor KEY_ID=0xADDRESS (repeatable)")
    s.add_argument("--template", "--prompt-template", dest="template", metavar="ID|FILE",
                   help="PR template: a built-in id (" + ", ".join(sorted(TEMPLATES)) + "), a template file "
                        "(.toml/.json), or the id of a file in $DOSR_TEMPLATES or ~/.dosr/templates. A custom "
                        "template is embedded in the policy (see `dosr template`)")
    s.add_argument("--max-changed-files", type=int)
    s.add_argument("--max-patch-bytes", type=int)
    s.add_argument("--minimum-approvals", type=int)
    s.add_argument("--context-file", action="append", metavar="GLOB",
                   help="extra file/glob every PR must include as context, e.g. 'docs/*.md' (repeatable)")
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

    s = sub.add_parser("submit", help="commit (optional), bundle, build PR + prompt, validate, request review, register")
    s.add_argument("-m", "--message", help="commit message for uncommitted changes")
    s.add_argument("--model", help="review model provider/model[@version], e.g. openai/gpt-4o, claude/claude-sonnet-4-5 "
                                   "(see `dosr models`; default: profile, then policy)")
    s.add_argument("--dry-run", action="store_true", help="build and validate locally, but send nothing")
    s.add_argument("--force", action="store_true",
                   help="send even if local validation fails (simulates a modified client; the attestor rejects)")
    s.add_argument("--no-register", action="store_true", help="do not submit an approval to the chain")
    s.add_argument("--no-publish", action="store_true", help="do not publish the new HEAD to IPFS after approval")
    s.add_argument("--bundle-mode", choices=["full", "thin"])
    s.add_argument("--bad-request", choices=sorted(BAD_REQUESTS), metavar="MODE",
                   help="DEMO: send a deliberately bad request so the attestor rejects it. MODE is one of: "
                        + "; ".join(f"{k} ({v})" for k, v in BAD_REQUESTS.items()))
    s.add_argument("--show-pr", action="store_true", help="print the generated PR afterwards")
    s.add_argument("--show-prompt", action="store_true", help="print the exact prompt sent to the attestor")
    s.add_argument("--show-checks", action="store_true", help="list every client/attestor check, not just failures")
    s.add_argument("--json", action="store_true", help="print the final result as JSON only")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("history", help="list past submissions")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("show", help="show a past submission (default: latest)")
    s.add_argument("request_id", nargs="?")
    s.add_argument("--pr", action="store_true", help="print the PR document")
    s.add_argument("--response", action="store_true", help="print the attestor response JSON")
    s.add_argument("--prompt", action="store_true", help="print the prompt sent to the attestor")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    sub.add_parser("server", help="check attestor health and keys").set_defaults(func=cmd_server)
    sub.add_parser("register", help="register an existing repo's genesis on the chain").set_defaults(func=cmd_register)
    sub.add_parser("chain", help="show the mock chain state").set_defaults(func=cmd_chain)

    s = sub.add_parser("publish", help="publish the canonical HEAD + signed history to IPFS; prints a share code")
    s.add_argument("--announce", action="append", metavar="PEER_URL",
                   help="also send the share code to a peer's inbox, e.g. http://192.168.1.20:8790 (repeatable)")
    s.set_defaults(func=cmd_publish)

    for name, helptext in (("import", "clone a shared repository (or update your copy) from a share code"),
                           ("pull", "update this repository from a share code (same as import)")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("share", help="share code: an IPFS CID (bafy…) or local:<sha256>")
        s.add_argument("--workspace", help="directory to clone into / look for the repo (import default: cwd)")
        s.add_argument("--expect-head", metavar="OID", help="fail unless the share is for this commit (prefix ok)")
        s.add_argument("--timeout", type=float, default=60, help="seconds to wait for IPFS to find the data")
        s.add_argument("--adopt", action="store_true",
                       help="DEV: on a conflict, discard this device's chain history for the repo and adopt the shared one")
        s.set_defaults(func=cmd_import)

    s = sub.add_parser("p2p", help="IPFS / local-store status (peer id, addresses, connected peers)")
    s.add_argument("--connect", action="append", metavar="MULTIADDR", help="dial another IPFS node directly")
    s.set_defaults(func=cmd_p2p)

    s = sub.add_parser("template", help="PR templates: list, show, create, check")
    tsub = s.add_subparsers(dest="template_cmd", required=True, metavar="ACTION")
    t = tsub.add_parser("list", help="built-in templates and template files found in the search directories")
    t = tsub.add_parser("show", help="show a template (default: the current repository's)")
    t.add_argument("template", nargs="?", metavar="ID|FILE")
    t.add_argument("--toml", action="store_true", help="print it as a TOML template file")
    t.add_argument("--preview", action="store_true", help="render it on a sample change")
    t = tsub.add_parser("new", help="write a new template file to edit, copied from an existing one")
    t.add_argument("id", help="new template id, e.g. team-review-v1")
    t.add_argument("--from", dest="base", metavar="ID|FILE", help="template to copy (default: standard-code-review-v1)")
    t.add_argument("--description")
    t.add_argument("-o", "--output", help="output file (default: ID.toml)")
    t.add_argument("--overwrite", action="store_true")
    t = tsub.add_parser("check", help="validate a template file (and preview it)")
    t.add_argument("file")
    t.add_argument("--preview", action="store_true", help="render it on a sample change")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("models", help="list common review models (OpenAI, Claude, Gemini, ...) and policy approval")
    s.add_argument("--provider", help="only this provider (openai, anthropic/claude, gemini/google, zhipu, mock)")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("profiles", help="list client profiles in a directory")
    s.add_argument("--dir", default="demo/clients")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("gui", help="launch the local web GUI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--workspace", default=".", help="directory containing DOSR repositories")
    s.add_argument("--seeds", help="directory of seed projects offered when creating repos")
    s.add_argument("--profiles", help="directory of client profiles to switch between")
    s.add_argument("--env-file", help="save API keys entered in the GUI to this .env file (default: this session only)")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--p2p-port", type=int, help="run the peer inbox (dev channel for share codes) on this port, "
                                                "reachable from the LAN; e.g. 8790")
    s.add_argument("--p2p-host", default="0.0.0.0", help="interface for the peer inbox (default: all)")
    s.add_argument("--device", help="name other devices see (default: $DOSR_DEVICE_NAME or the hostname)")
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
    except (ConfigError, PolicyError, PRFormatError, TemplateError, ChainError, AttestorError, GitError,
            FileNotFoundError, P2PError, StoreError, ValueError) as e:
        err.print(f"[bold red]error:[/] {e}")
        return 1
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
