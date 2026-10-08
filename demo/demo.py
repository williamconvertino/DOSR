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


def attestor_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def attestor_up(port: int) -> bool:
    import httpx

    try:
        return httpx.get(attestor_url(port) + "/v1/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def start_attestor_process(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ATTESTOR_DIR,
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
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(args.port)], cwd=ATTESTOR_DIR
    )


def _gui_argv(args):
    argv = ["--chain-state", str(CHAIN_STATE), "--attestor-url", attestor_url(args.attestor_port), "gui",
            "--workspace", str(WORKSPACE), "--seeds", str(PROJECTS), "--profiles", str(CLIENTS),
            "--port", str(args.port)]
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

    seed_meta = lambda s: json.loads((PROJECTS / s / "seed.json").read_text("utf-8"))

    try:
        # 1. Alice creates calculator-py ---------------------------------------
        meta = seed_meta("calculator-py")
        scenario("1. Create repository calculator-py", "Alice",
                 "git init + seed files + immutable .dosr/policy.json, genesis commit, register on the mock chain.")
        as_client("alice", "init", str(WORKSPACE / "calculator-py"), "--name", "calculator-py",
                  "--description", meta["description"], "--preset", meta["preset"],
                  "--seed", str(PROJECTS / "calculator-py" / "files"))
        results.append(("Create calculator-py", "Alice", "created", "created", None))

        # 2. Bob proposes a good change ----------------------------------------
        ch = meta["demo_changes"][0]
        scenario("2. Propose a change (power/modulo)", "Bob",
                 "Edit files, then `dosr submit -m`: commit → bundle → PR → attestor → verify → register.")
        apply_change("calculator-py", ch["id"], "calculator-py")
        as_client("bob", "status", repo="calculator-py")
        as_client("bob", "submit", "-m", ch["message"], repo="calculator-py")
        record("Bob: add power()/modulo()", "Bob", ch["expect"], "calculator-py")

        # 3. Mallory tries an unapproved model ----------------------------------
        ch = meta["demo_changes"][1]
        scenario("3. Request review from an unapproved model", "Mallory",
                 "Mallory's client asks for `mock/always-approve@1`. The attestor checks the policy and rejects; "
                 "canonical HEAD does not move.")
        apply_change("calculator-py", ch["id"], "calculator-py")
        as_client("mallory", "submit", "-m", ch["message"], repo="calculator-py")
        record("Mallory: unapproved model", "Mallory", ch["expect"], "calculator-py")
        # Drop Mallory's rejected local commit so later scenarios start from canonical HEAD.
        git = GitRepo(WORKSPACE / "calculator-py")
        git.run("reset", "-q", "--hard", "HEAD~1")

        # 4. Stale parent ---------------------------------------------------------
        scenario("4. Stale parent (built on an old HEAD)", "Bob",
                 "Bob branches from the genesis commit, which is no longer canonical. The client refuses before "
                 "contacting the attestor (the contract would reject it anyway).")
        root = git.run("rev-list", "--max-parents=0", "HEAD").split()[0]
        git.run("checkout", "-q", "-b", "stale-work", root)
        (WORKSPACE / "calculator-py" / "NOTES.md").write_text("Some notes written against an old HEAD.\n")
        as_client("bob", "submit", "-m", "Add notes", repo="calculator-py")
        record("Bob: stale parent", "Bob", "error", "calculator-py")
        git.run("checkout", "-q", "-f", "main")
        git.run("branch", "-q", "-D", "stale-work")

        # 5. Carol: todo-js with two commits ---------------------------------------
        meta = seed_meta("todo-js")
        ch = meta["demo_changes"][0]
        scenario("5. New repo todo-js + multi-commit PR", "Carol",
                 "Carol creates a second repo, commits twice locally, then submits both commits as one PR.")
        as_client("carol", "init", str(WORKSPACE / "todo-js"), "--name", "todo-js",
                  "--description", meta["description"], "--preset", meta["preset"],
                  "--seed", str(PROJECTS / "todo-js" / "files"))
        apply_change("todo-js", ch["id"], "todo-js")
        as_client("carol", "commit", "-m", "Add complete() and open()", repo="todo-js")
        with open(WORKSPACE / "todo-js" / "README.md", "a", encoding="utf-8") as f:
            f.write("\nCompleted items can be filtered out with `list.open()`.\n")
        as_client("carol", "submit", "-m", ch["message"], repo="todo-js")
        record("Carol: complete() (2 commits)", "Carol", ch["expect"], "todo-js")

        # 6. secure-vault under a strict policy --------------------------------------
        meta = seed_meta("secure-vault")
        ch = meta["demo_changes"][0]
        scenario("6. Strict policy: attestor not trusted by the repo", "Alice",
                 "secure-vault only trusts attestor-a/attestor-b, so our local attestor rejects the review "
                 "(rule: this attestor's key must be listed in the policy).")
        as_client("alice", "init", str(WORKSPACE / "secure-vault"), "--name", "secure-vault",
                  "--description", meta["description"], "--preset", meta["preset"],
                  "--seed", str(PROJECTS / "secure-vault" / "files"))
        apply_change("secure-vault", ch["id"], "secure-vault")
        as_client("alice", "submit", "-m", ch["message"],
                  "--model", "provider-a/review-model-a@pinned-version", repo="secure-vault")
        record("Alice: secure-vault change", "Alice", ch["expect"], "secure-vault")

        # Summary -------------------------------------------------------------------
        console.print()
        console.print(Rule("[bold]Summary"))
        as_client("alice", "chain")
        t = Table(title="Scenario outcomes")
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
