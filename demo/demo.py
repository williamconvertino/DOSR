"""DOSR demo launcher. Run with the demo venv's Python (see demo/README.md).

    python demo/demo.py attestor        start the attestor (foreground)
    python demo/demo.py gui             start the client GUI on the demo workspace
    python demo/demo.py up              attestor in the background + GUI (one terminal)
    python demo/demo.py run             scripted end-to-end CLI demo (several fake clients)
    python demo/demo.py reset           delete demo/workspace (repos + mock chain)
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
WORKSPACE = DEMO / "workspace"
CHAIN_STATE = WORKSPACE / ".chain" / "state.json"
PROJECTS = DEMO / "projects"
CLIENTS = DEMO / "clients"
TEMPLATES = DEMO / "templates"  # custom PR template files (found by id via DOSR_TEMPLATES)
ATTESTOR_DIR = ROOT / "services" / "attestor"


def _require_deps():
    try:
        import dosr_client  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("DOSR packages are not installed in this Python.\n"
              "Run `python demo/setup_env.py`, then use demo/.venv's Python "
              "(Windows: demo\\.venv\\Scripts\\python.exe, macOS/Linux: demo/.venv/bin/python).",
              file=sys.stderr)
        sys.exit(1)


def load_dotenv() -> None:
    """Export the repo's .env (gitignored; e.g. OPENAI_API_KEY) to this process and the
    attestor/client processes it starts. Values already in the environment win."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def attestor_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def attestor_up(port: int) -> bool:
    import httpx

    try:
        return httpx.get(attestor_url(port) + "/v1/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def attestor_env() -> dict:
    # The attestor reads canonical HEAD / policy hashes from the same mock chain as the clients.
    return {**os.environ, "DOSR_CHAIN_STATE": str(CHAIN_STATE),
            "ATTESTOR_CACHE_DIR": str(WORKSPACE / ".attestor-cache")}


def start_attestor_process(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ATTESTOR_DIR, env=attestor_env(),
    )
    for _ in range(60):
        if attestor_up(port):
            return proc
        if proc.poll() is not None:
            raise SystemExit(f"attestor exited with code {proc.returncode} (is port {port} in use?)")
        time.sleep(0.25)
    proc.terminate()
    raise SystemExit("attestor did not become healthy within 15s")


# ---------------------------------------------------------------- commands


def cmd_attestor(args):
    print(f"Starting DOSR attestor on {attestor_url(args.port)} (docs: {attestor_url(args.port)}/docs)")
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(args.port)], cwd=ATTESTOR_DIR,
        env=attestor_env(),
    )


def _gui_argv(args):
    argv = ["--chain-state", str(CHAIN_STATE), "--attestor-url", attestor_url(args.attestor_port), "gui",
            "--workspace", str(WORKSPACE), "--seeds", str(PROJECTS), "--profiles", str(CLIENTS),
            "--port", str(args.port), "--env-file", str(ROOT / ".env")]
    if args.no_browser:
        argv.append("--no-browser")
    return argv


def cmd_gui(args):
    from dosr_client.cli import main as dosr

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if not attestor_up(args.attestor_port):
        print(f"note: no attestor at {attestor_url(args.attestor_port)} yet; start it with `demo.py attestor`")
    return dosr(_gui_argv(args))


def cmd_up(args):
    from dosr_client.cli import main as dosr

    proc = None
    if attestor_up(args.attestor_port):
        print(f"Using already-running attestor at {attestor_url(args.attestor_port)}")
    else:
        print(f"Starting attestor on {attestor_url(args.attestor_port)}...")
        proc = start_attestor_process(args.attestor_port)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    try:
        return dosr(_gui_argv(args))
    finally:
        if proc:
            proc.terminate()


def cmd_reset(args):
    if WORKSPACE.exists():
        def retry_writable(func, path, _):  # git marks pack files read-only on Windows
            os.chmod(path, 0o700)
            func(path)
        key = "onexc" if sys.version_info >= (3, 12) else "onerror"
        shutil.rmtree(WORKSPACE, **{key: retry_writable})
        print(f"Deleted {WORKSPACE}")
    else:
        print("Nothing to reset.")
    return 0


# ---------------------------------------------------------------- scripted demo


# Real-LLM model for the llm-review scenarios: the first provider whose key is set
# (DEMO_LLM_MODEL=provider/model@version overrides). Each is a model that supports the
# attestor's fixed parameters (temperature 0, JSON output).
LLM_DEFAULTS = [
    ("ANTHROPIC_API_KEY", "anthropic/claude-sonnet-4-5@20250929"),
    ("OPENAI_API_KEY", "openai/gpt-4.1@2025-04-14"),
    ("GEMINI_API_KEY", "gemini/gemini-2.5-flash@stable"),
    ("GOOGLE_API_KEY", "gemini/gemini-2.5-flash@stable"),
    ("ZHIPU_API_KEY", "zhipu/glm-4-plus@api-v4"),
]
LLM_KEYS = [k for k, _ in LLM_DEFAULTS]


def pick_llm_model() -> str | None:
    if os.environ.get("DEMO_LLM_MODEL"):
        return os.environ["DEMO_LLM_MODEL"]
    return next((model for env, model in LLM_DEFAULTS if os.environ.get(env)), None)


def cmd_run(args):
    import json

    from rich.console import Console
    from rich.rule import Rule
    from rich.table import Table

    from dosr_client.cli import main as dosr
    from dosr_git import GitRepo

    console = Console(highlight=False)
    if args.reset:
        cmd_reset(args)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    proc = None
    if not attestor_up(args.attestor_port):
        if not args.start_attestor:
            console.print(f"[red]No attestor at {attestor_url(args.attestor_port)}.[/] Start one with "
                          "`demo.py attestor`, or pass --start-attestor.")
            return 1
        console.print(f"Starting attestor on {attestor_url(args.attestor_port)}...")
        proc = start_attestor_process(args.attestor_port)

    for name in ("calculator-py", "todo-js", "secure-vault"):
        if (WORKSPACE / name).exists():
            console.print(f"[red]{WORKSPACE / name} already exists.[/] Re-run with --reset to start fresh.")
            return 1

    base = ["--chain-state", str(CHAIN_STATE), "--attestor-url", attestor_url(args.attestor_port)]
    results = []
    llm = pick_llm_model()

    def as_client(client, *argv, repo=None):
        full = [*base, "--profile", str(CLIENTS / f"{client}.json")]
        if repo:
            full += ["-C", str(WORKSPACE / repo)]
        return dosr([*full, *argv])

    def apply_change(seed, change_id, repo):
        src = PROJECTS / seed / "changes" / change_id
        shutil.copytree(src, WORKSPACE / repo, dirs_exist_ok=True)

    def latest(repo):
        runs = sorted((WORKSPACE / repo / ".git" / "dosr" / "submissions").glob("*/run.json"),
                      key=lambda p: p.stat().st_mtime)
        return json.loads(runs[-1].read_text("utf-8")) if runs else None

    def scenario(title, who, explain):
        console.print()
        console.print(Rule(f"[bold]{title}[/]  ·  as [cyan]{who}[/]"))
        console.print(f"[dim]{explain}[/]")
        if args.pause:
            input("  press Enter to continue...")

    def record(title, who, expect, repo):
        run = latest(repo)
        results.append((title, who, expect, run["outcome"] if run else "—", run["elapsed_ms"] if run else None))

    def seed_meta(seed):
        return json.loads((PROJECTS / seed / "seed.json").read_text("utf-8"))

    def init_from_seed(client, seed, preset=None, models=None):
        meta = seed_meta(seed)
        over = dict(meta.get("policy_overrides", {}))
        if models:
            over["approved_models"] = models
        description = meta["description"]
        if preset and preset != meta["preset"]:
            description = f"{description.split('.')[0]}. Running on the {preset} preset (no LLM key set)."
        argv = ["init", str(WORKSPACE / seed), "--name", seed, "--description", description,
                "--preset", preset or meta["preset"], "--seed", str(PROJECTS / seed / "files")]
        for m in over.get("approved_models", []):
            argv += ["--model", m]
        for g in over.get("context_files", []):
            argv += ["--context-file", g]
        if over.get("template"):
            argv += ["--template", over["template"]]
        as_client(client, *argv)

    def drop_last_commit(repo):
        GitRepo(WORKSPACE / repo).run("reset", "-q", "--hard", "HEAD~1")

    try:
        # 1. create ------------------------------------------------------------------
        meta = seed_meta("calculator-py")
        # The llm-review policy needs the contributor's key for one LLM provider; it approves the
        # model of whichever provider has a key. Without any key, fall back to the mock reviewer.
        calc_preset = meta["preset"] if llm else "standard"
        scenario("1. Create repository calculator-py", "Alice",
                 "git init + seed files + immutable .dosr/policy.json, genesis commit, register on the mock chain "
                 "(a local file standing in for the contract). The policy adds docs/*.md as required review "
                 "context. " + (f"Policy llm-review: PRs must be reviewed by {llm}."
                                if llm else f"No LLM key ({', '.join(LLM_KEYS)}): using the standard (mock "
                                            "reviewer) policy."))
        init_from_seed("alice", "calculator-py", calc_preset, models=[llm] if llm else None)
        results.append(("Create calculator-py", "Alice", "created", "created", None))

        # 2. honest PR ---------------------------------------------------------------
        ch = meta["demo_changes"][0]
        scenario("2. Propose a change (power/modulo)", "Bob",
                 "`dosr submit -m`: commit → bundle → PR → prompt (template + docs/CONTRIBUTING.md) → validate "
                 "locally (the attestor's checks, run on the exact bytes) → the attestor rebuilds PR + prompt from "
                 "the bundle, checks they match ours, " + (f"{llm} reviews it" if llm else "the mock reviewer approves")
                 + " → signed attestation → verify → register.")
        apply_change("calculator-py", ch["id"], "calculator-py")
        as_client("bob", "submit", "-m", ch["message"], "--show-checks", repo="calculator-py")
        record("Bob: add power()/modulo()", "Bob", "approved", "calculator-py")

        # 3-4. unapproved model: client blocks, forced → attestor rejects -------------
        ch = meta["demo_changes"][1]
        scenario("3. Unapproved model: the client stops it", "Mallory",
                 "Mallory's profile asks for `mock/always-approve@1`. Local validation fails model_is_approved, "
                 "so nothing is sent.")
        apply_change("calculator-py", ch["id"], "calculator-py")
        as_client("mallory", "submit", "-m", ch["message"], repo="calculator-py")
        record("Mallory: unapproved model", "Mallory", "blocked", "calculator-py")

        scenario("4. ...forced through with a modified client: the attestor rejects", "Mallory",
                 "`--force` skips local validation (as a modified client would). The attestor runs the same checks "
                 "itself and returns a signed REJECTED attestation; canonical HEAD does not move.")
        as_client("mallory", "submit", "--force", repo="calculator-py")
        record("Mallory: unapproved model, forced", "Mallory", "rejected", "calculator-py")
        drop_last_commit("calculator-py")

        # 5. honest mistake: stale branch ----------------------------------------------
        git = GitRepo(WORKSPACE / "calculator-py")
        scenario("5. Stale parent (work built on an old HEAD)", "Bob",
                 "Bob branches from the genesis commit, which is no longer canonical. The client refuses before "
                 "building anything; the contract would reject it anyway.")
        root = git.run("rev-list", "--max-parents=0", "HEAD").split()[0]
        git.run("checkout", "-q", "-b", "stale-work", root)
        (WORKSPACE / "calculator-py" / "NOTES.md").write_text("Some notes written against an old HEAD.\n")
        as_client("bob", "submit", "-m", "Add notes", repo="calculator-py")
        record("Bob: stale branch", "Bob", "error", "calculator-py")
        git.run("checkout", "-q", "-f", "main")
        git.run("branch", "-q", "-D", "stale-work")

        # 6. tampered requests: blocked, then forced → rejected -------------------------
        tampering = [
            ("stale-parent", "head mismatch: claims an older parent than the canonical HEAD"),
            ("bundle-hash-mismatch", "checksum mismatch: bundle bytes differ from the sha256 in the request"),
            ("wrong-prompt-template", "prompt template mismatch: prompt built with another template"),
            ("tampered-prompt", "prompt injection: a 'pre-approved' note added to the prompt"),
            ("modified-policy", "loosened policy: extra always-approve model, hash recomputed to match"),
        ]
        scenario("6. Tampered requests: blocked by the client, rejected by the attestor when forced", "Bob",
                 "Each bad-request mode corrupts one part of an otherwise valid PR. Without --force the client's "
                 "validation stops it; with --force the attestor rejects it (signed, before any model call).")
        (WORKSPACE / "calculator-py" / "CHANGELOG.md").write_text("- power() and modulo()\n")
        as_client("bob", "commit", "-m", "Add changelog", repo="calculator-py")
        for mode, what in tampering:
            console.print(Rule(f"[yellow]{mode}[/]: {what}", style="dim"))
            as_client("bob", "submit", "--bad-request", mode, repo="calculator-py")
            record(f"Bob: {mode}", "Bob", "blocked", "calculator-py")
            as_client("bob", "submit", "--bad-request", mode, "--force", repo="calculator-py")
            record(f"Bob: {mode}, forced", "Bob", "rejected", "calculator-py")
        drop_last_commit("calculator-py")

        # 7. todo-js: version ranges + multi-commit PR ------------------------------------
        meta = seed_meta("todo-js")
        ch = meta["demo_changes"][0]
        scenario("7. New repo todo-js: custom PR template + minimum model version", "Carol",
                 "The policy approves mock/deterministic-reviewer@^1.2.0 (>=1.2.0 <2.0.0), requires docs/*.md + "
                 "package.json as context, and uses the custom PR template demo/templates/team-review-v1.toml "
                 "(embedded in the policy, so its hash is fixed at genesis; the attestor lays out the PR with it). "
                 "Carol commits twice and submits both commits as one PR, first asking for an outdated 1.1.0, "
                 "then for 1.4.2.")
        init_from_seed("carol", "todo-js")
        apply_change("todo-js", ch["id"], "todo-js")
        as_client("carol", "commit", "-m", "Add complete() and open()", repo="todo-js")
        with open(WORKSPACE / "todo-js" / "README.md", "a", encoding="utf-8") as f:
            f.write("\nCompleted items can be filtered out with `list.open()`.\n")
        as_client("carol", "submit", "-m", ch["message"], "--model", "mock/deterministic-reviewer@1.1.0",
                  repo="todo-js")
        record("Carol: model 1.1.0 (below ^1.2.0)", "Carol", "blocked", "todo-js")
        as_client("carol", "submit", "--model", "mock/deterministic-reviewer@1.1.0", "--force", repo="todo-js")
        record("Carol: model 1.1.0, forced", "Carol", "rejected", "todo-js")
        console.print(Rule("[green]same commits, model 1.4.2 (inside ^1.2.0)[/]", style="dim"))
        as_client("carol", "submit", "--model", "mock/deterministic-reviewer@1.4.2", repo="todo-js")
        record("Carol: model 1.4.2, 2 commits", "Carol", "approved", "todo-js")

        # 8. strict policy ------------------------------------------------------------------
        meta = seed_meta("secure-vault")
        ch = meta["demo_changes"][0]
        scenario("8. Strict policy: this attestor is not trusted by the repo", "Alice",
                 "secure-vault only trusts attestor-a/attestor-b. The client sees (via /v1/keys) that our attestor "
                 "is not listed and blocks; forced, the attestor itself refuses to approve.")
        init_from_seed("alice", "secure-vault")
        apply_change("secure-vault", ch["id"], "secure-vault")
        model = ["--model", "provider-a/review-model-a@pinned-version"]
        as_client("alice", "submit", "-m", ch["message"], *model, repo="secure-vault")
        record("Alice: secure-vault change", "Alice", "blocked", "secure-vault")
        as_client("alice", "submit", *model, "--force", repo="secure-vault")
        record("Alice: secure-vault change, forced", "Alice", "rejected", "secure-vault")

        # 9. backdoor caught by the LLM --------------------------------------------------------
        ch = seed_meta("calculator-py")["demo_changes"][2]
        if not llm:
            console.print()
            console.print(Rule("[bold]9. LLM review scenario skipped"))
            console.print(f"[yellow]calculator-py ran on the mock reviewer. Set one of {', '.join(LLM_KEYS)} "
                          "(environment or the repo's .env) to have a real LLM review its PRs.[/]")
        else:
            scenario("9. Backdoor caught by the LLM review", "Mallory",
                     "Mallory adds evaluate(), which runs arbitrary Python through eval() with full builtins, and a "
                     "comment telling the reviewer the PR is pre-approved. Every deterministic check passes (approved "
                     "model, correct prompt), so the attestor calls the LLM, which rejects it; the signed rejection "
                     "lists the issues.")
            apply_change("calculator-py", ch["id"], "calculator-py")
            as_client("mallory", "submit", "-m", ch["message"], "--model", llm, repo="calculator-py")
            record(f"Mallory: eval() backdoor via {llm.split('/')[0]}", "Mallory", ch["expect"], "calculator-py")
            drop_last_commit("calculator-py")

        # Summary ---------------------------------------------------------------------------------
        console.print()
        console.print(Rule("[bold]Summary"))
        as_client("alice", "chain")
        t = Table(title="Scenario outcomes  (blocked = stopped by the client before sending; "
                        "rejected = signed rejection from the attestor)")
        for col in ("Scenario", "Client", "Expected", "Actual", "Total time"):
            t.add_column(col)
        all_ok = True
        for title, who, expect, actual, elapsed in results:
            ok = expect == actual
            all_ok &= ok
            t.add_row(title, who, expect, f"[{'green' if ok else 'red'}]{actual}[/]",
                      "" if elapsed is None else f"{elapsed:.0f} ms")
        console.print(t)
        console.print(f"\nOpen the GUI on this workspace with: [bold]{Path(sys.executable).name} demo/demo.py gui[/]")
        return 0 if all_ok else 1
    finally:
        if proc:
            proc.terminate()


def main():
    load_dotenv()
    os.environ["DOSR_TEMPLATES"] = os.pathsep.join(
        p for p in (os.environ.get("DOSR_TEMPLATES"), str(TEMPLATES)) if p)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--attestor-port", type=int, default=8080)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("attestor", help="run the attestor in the foreground")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_attestor)

    for name, fn, helptext in (("gui", cmd_gui, "run the client GUI"), ("up", cmd_up, "attestor + GUI together")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--no-browser", action="store_true")
        s.set_defaults(func=fn)

    s = sub.add_parser("run", help="scripted end-to-end CLI demo")
    s.add_argument("--start-attestor", action="store_true", help="start an attestor if none is running")
    s.add_argument("--reset", action="store_true", help="wipe demo/workspace first")
    s.add_argument("--pause", action="store_true", help="wait for Enter between scenarios (for presenting)")
    s.set_defaults(func=cmd_run)

    sub.add_parser("reset", help="delete demo/workspace").set_defaults(func=cmd_reset)

    args = ap.parse_args()
    if args.cmd != "reset":
        _require_deps()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
