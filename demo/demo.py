"""DOSR demo launcher. Run with the demo venv's Python (see demo/README.md).

    python demo/demo.py attestor        start the attestor (foreground)
    python demo/demo.py gui             start the client GUI on the demo workspace
    python demo/demo.py up              IPFS + attestor in the background + GUI (one terminal)
    python demo/demo.py run             scripted end-to-end CLI demo (several fake clients)
    python demo/demo.py reset           delete demo/workspace (repos + mock chain)
    python demo/demo.py ipfs install    download IPFS (Kubo) into demo/.ipfs (once)
    python demo/demo.py ipfs            run the IPFS node (foreground); `up` starts it for you
    python demo/demo.py pair            two clients on this computer (devices a + b), already peered

`--device b` (before the command) runs a second, separate device on the same computer:
its own workspace (demo/workspace-b), mock chain, attestor (:8081), GUI (:8766), peer
inbox (:8791) and IPFS node (API :5002, swarm :4002). Use it to try P2P sharing without a
second computer; `pair` starts devices a and b together.
"""

import argparse
import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
WORKSPACE = DEMO / "workspace"  # device a (default); --device b uses demo/workspace-b
CHAIN_STATE = WORKSPACE / ".chain" / "state.json"
PROJECTS = DEMO / "projects"
CLIENTS = DEMO / "clients"
TEMPLATES = DEMO / "templates"  # custom PR template files (found by id via DOSR_TEMPLATES)
ATTESTOR_DIR = ROOT / "services" / "attestor"

# IPFS (Kubo). Binary + node repo live in demo/.ipfs (git-ignored). The node's gateway is
# moved off :8080, which the attestor uses.
IPFS_DIR = DEMO / ".ipfs"
LOCAL_STORE = IPFS_DIR / "local-store"  # fallback store when no IPFS node runs (shared by local devices)
KUBO_VERSION = "v0.43.1"
IPFS_API = "http://127.0.0.1:5001"  # device a's node (the default everywhere else, e.g. IPFS Desktop)


class Device:
    """One simulated computer: workspace, mock chain, attestor, GUI and peer-inbox ports."""

    def __init__(self, name: str = "a"):
        if len(name) != 1 or not name.isalpha():
            raise SystemExit("--device must be a single letter (a, b, ...)")
        self.name = name.lower()
        i = ord(self.name) - ord("a")
        self.workspace = DEMO / ("workspace" if i == 0 else f"workspace-{self.name}")
        self.chain_state = self.workspace / ".chain" / "state.json"
        self.attestor_port = 8080 + i
        self.gui_port = 8765 + i
        self.inbox_port = 8790 + i
        # Each device has its own IPFS node, so even on one computer the data really moves
        # between two nodes. Device a uses the standard ports (and any node already there).
        self.ipfs_repo = IPFS_DIR / ("repo" if i == 0 else f"repo-{self.name}")
        self.ipfs_api_port = 5001 + i
        self.ipfs_api = f"http://127.0.0.1:{self.ipfs_api_port}"
        self.ipfs_swarm_port = 4001 + i
        self.ipfs_gateway_port = 8089 + i

    @property
    def label(self) -> str:
        return f"device-{self.name}"


DEVICE = Device("a")  # replaced from --device in main()


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


def attestor_env(device: Device | None = None) -> dict:
    # The attestor reads canonical HEAD / policy hashes from the same mock chain as the clients.
    device = device or DEVICE
    return {**os.environ, "DOSR_CHAIN_STATE": str(device.chain_state),
            "ATTESTOR_CACHE_DIR": str(device.workspace / ".attestor-cache")}


def start_attestor_process(port: int, device: Device | None = None) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ATTESTOR_DIR, env=attestor_env(device),
    )
    for _ in range(60):
        if attestor_up(port):
            return proc
        if proc.poll() is not None:
            raise SystemExit(f"attestor exited with code {proc.returncode} (is port {port} in use?)")
        time.sleep(0.25)
    proc.terminate()
    raise SystemExit("attestor did not become healthy within 15s")


# ---------------------------------------------------------------- IPFS (Kubo)


def ipfs_binary() -> Path | None:
    local = IPFS_DIR / "bin" / ("ipfs.exe" if sys.platform == "win32" else "ipfs")
    if local.exists():
        return local
    found = shutil.which("ipfs")
    return Path(found) if found else None


def ipfs_up(api: str | None = None) -> bool:
    import httpx

    try:
        return httpx.post((api or DEVICE.ipfs_api) + "/api/v0/id",
                          timeout=httpx.Timeout(2, connect=0.5)).status_code == 200
    except httpx.HTTPError:
        return False


def _kubo_asset() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if sys.platform == "win32":
        return f"kubo_{KUBO_VERSION}_windows-{arch}.zip"
    osname = "darwin" if sys.platform == "darwin" else "linux"
    return f"kubo_{KUBO_VERSION}_{osname}-{arch}.tar.gz"


def ipfs_install(force: bool = False) -> Path:
    """Download the official Kubo release from GitHub, check its sha512, unpack to demo/.ipfs/bin."""
    import httpx

    existing = ipfs_binary()
    if existing and not force:
        print(f"IPFS already available: {existing}")
        return existing
    asset = _kubo_asset()
    base = f"https://github.com/ipfs/kubo/releases/download/{KUBO_VERSION}/{asset}"
    print(f"Downloading {base} ...")
    data = httpx.get(base, follow_redirects=True, timeout=300).raise_for_status().content
    expected = httpx.get(base + ".sha512", follow_redirects=True, timeout=60).raise_for_status().text.split()[0]
    if hashlib.sha512(data).hexdigest() != expected.lower():
        raise SystemExit("downloaded Kubo archive does not match its published sha512; not installing")
    name = "ipfs.exe" if sys.platform == "win32" else "ipfs"
    if asset.endswith(".zip"):
        member = zipfile.ZipFile(io.BytesIO(data)).read(f"kubo/{name}")
    else:
        member = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").extractfile(f"kubo/{name}").read()
    out = IPFS_DIR / "bin" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(member)
    out.chmod(0o755)
    print(f"Installed IPFS (Kubo {KUBO_VERSION}) at {out}")
    return out


def _ipfs_env(device: Device) -> dict:
    return {**os.environ, "IPFS_PATH": str(device.ipfs_repo)}


def ipfs_prepare(binary: Path, device: Device) -> None:
    """`ipfs init` once; set this device's ports (the gateway stays off the attestor's :8080)."""
    env = _ipfs_env(device)
    if not (device.ipfs_repo / "config").exists():
        print(f"Initializing IPFS node in {device.ipfs_repo} ...")
        device.ipfs_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(binary), "init", "--empty-repo"], env=env, check=True, stdout=subprocess.DEVNULL)
    sp = device.ipfs_swarm_port
    settings = [
        ("Addresses.API", f"/ip4/127.0.0.1/tcp/{device.ipfs_api_port}"),
        ("Addresses.Gateway", f"/ip4/127.0.0.1/tcp/{device.ipfs_gateway_port}"),
        ("--json", "Addresses.Swarm", f'["/ip4/0.0.0.0/tcp/{sp}", "/ip6/::/tcp/{sp}", '
                                      f'"/ip4/0.0.0.0/udp/{sp}/quic-v1", "/ip6/::/udp/{sp}/quic-v1"]'),
        ("--json", "Discovery.MDNS.Enabled", "true"),
    ]
    for setting in settings:
        subprocess.run([str(binary), "config", *setting], env=env, check=True)


def start_ipfs_process(device: Device | None = None, install: bool = True) -> subprocess.Popen | None:
    """Start the device's IPFS node in the background unless one is running (installing
    Kubo first if needed). None when IPFS is unavailable: sharing then uses the local store."""
    device = device or DEVICE
    if ipfs_up(device.ipfs_api):
        print(f"Using already-running IPFS node at {device.ipfs_api}")
        return None
    binary = ipfs_binary()
    if not binary and install:
        print("IPFS is not installed yet; downloading it once (about 40 MB)...")
        try:
            binary = ipfs_install()
        except (Exception, SystemExit) as e:
            print(f"  could not install IPFS ({e}).")
    if not binary:
        print("IPFS is not available: sharing uses the local store (this computer only). "
              "Run `demo.py ipfs install` to share with other devices.")
        return None
    ipfs_prepare(binary, device)
    print(f"Starting IPFS node for device {device.name} (API {device.ipfs_api}, swarm port "
          f"{device.ipfs_swarm_port})...")
    log_path = IPFS_DIR / f"daemon-{device.name}.log"
    log = open(log_path, "ab")
    proc = subprocess.Popen([str(binary), "daemon"], env=_ipfs_env(device), stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        if ipfs_up(device.ipfs_api):
            print(f"IPFS node up at {device.ipfs_api} (log: {log_path})")
            return proc
        if proc.poll() is not None:
            print(f"IPFS node exited (code {proc.returncode}); see {log_path}. Continuing with the local store.")
            return None
        time.sleep(0.25)
    print("IPFS node did not answer within 30 s; continuing (it may come up later).")
    return proc


def cmd_ipfs(args):
    if args.action == "install":
        ipfs_install(force=args.force)
        return 0
    if args.action == "status":
        print(f"binary : {ipfs_binary() or 'not installed (demo.py ipfs install)'}")
        print(f"repo   : {DEVICE.ipfs_repo}")
        print(f"API    : {DEVICE.ipfs_api} ({'up' if ipfs_up() else 'not running'})")
        return 0
    if ipfs_up():
        print(f"An IPFS node is already running at {DEVICE.ipfs_api}.")
        return 0
    binary = ipfs_binary() or ipfs_install()
    ipfs_prepare(binary, DEVICE)
    print(f"Starting IPFS (API {DEVICE.ipfs_api}); Ctrl+C to stop.")
    return subprocess.call([str(binary), "daemon"], env=_ipfs_env(DEVICE))


# ---------------------------------------------------------------- commands


def cmd_attestor(args):
    print(f"Starting DOSR attestor on {attestor_url(args.port)} (docs: {attestor_url(args.port)}/docs)")
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "attestor.main:app", "--port", str(args.port)], cwd=ATTESTOR_DIR,
        env=attestor_env(),
    )


def _gui_argv(args):
    argv = ["--chain-state", str(CHAIN_STATE), "--attestor-url", attestor_url(args.attestor_port),
            "--ipfs-api", DEVICE.ipfs_api, "--local-store", str(LOCAL_STORE), "gui",
            "--workspace", str(WORKSPACE), "--seeds", str(PROJECTS), "--profiles", str(CLIENTS),
            "--port", str(args.port), "--env-file", str(ROOT / ".env"), "--device", args.device_name]
    if args.p2p_port:
        argv += ["--p2p-port", str(args.p2p_port)]
        if args.p2p_host:
            argv += ["--p2p-host", args.p2p_host]
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

    procs = []
    if not args.no_ipfs:
        ipfs = start_ipfs_process(DEVICE)
        if ipfs:
            procs.append(ipfs)
    if attestor_up(args.attestor_port):
        print(f"Using already-running attestor at {attestor_url(args.attestor_port)}")
    else:
        print(f"Starting attestor on {attestor_url(args.attestor_port)}...")
        procs.append(start_attestor_process(args.attestor_port))
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    try:
        return dosr(_gui_argv(args))
    finally:
        for proc in procs:
            proc.terminate()


def cmd_pair(args):
    """Two clients on this computer (devices a and b), each with its own workspace, mock
    chain, attestor and IPFS node, already peered with each other, one browser tab each."""
    import httpx
    import webbrowser

    if len(args.devices) != 2 or args.devices[0] == args.devices[1]:
        raise SystemExit("--devices takes two different letters, e.g. ab")
    devices = [Device(args.devices[0]), Device(args.devices[1])]
    procs: list[subprocess.Popen] = []

    def stop_all():
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()

    try:
        for dev in devices:
            dev.workspace.mkdir(parents=True, exist_ok=True)
            if not args.no_ipfs:
                node = start_ipfs_process(dev)
                if node:
                    procs.append(node)
            if attestor_up(dev.attestor_port):
                print(f"Using already-running attestor at {attestor_url(dev.attestor_port)}")
            else:
                print(f"Starting attestor for device {dev.name} on {attestor_url(dev.attestor_port)}...")
                procs.append(start_attestor_process(dev.attestor_port, dev))
            # Peer inbox on loopback only: both clients are on this computer (no firewall prompt).
            procs.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--device", dev.name, "gui",
                                           "--no-browser", "--p2p-host", "127.0.0.1",
                                           "--name", f"Laptop {dev.name.upper()}"]))
        guis = [httpx.Client(base_url=f"http://127.0.0.1:{d.gui_port}", timeout=30) for d in devices]
        for dev, gui in zip(devices, guis):
            for _ in range(120):
                try:
                    if gui.get("/api/p2p").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if any(p.poll() is not None for p in procs[-2:]):
                    raise SystemExit(f"device {dev.name}'s GUI exited (is port {dev.gui_port} in use?)")
                time.sleep(0.25)
            else:
                raise SystemExit(f"device {dev.name}'s GUI did not start")

        a, b = guis
        a.post("/api/profile", json={"id": "alice"})  # who is acting on each laptop (switchable in the GUI)
        b.post("/api/profile", json={"id": "bob"})
        peered = b.post("/api/p2p/peers", json={"url": f"http://127.0.0.1:{devices[0].inbox_port}"})
        if peered.status_code != 200:
            print(f"warning: could not peer the two clients: {peered.text}")
        # The two IPFS nodes: dial directly over loopback (works even with no network).
        pa = a.get("/api/p2p").json()
        if pa.get("backend") == "ipfs" and b.get("/api/p2p").json().get("backend") == "ipfs":
            addr = f"/ip4/127.0.0.1/tcp/{devices[0].ipfs_swarm_port}/p2p/{pa['ipfs']['peer_id']}"
            res = b.post("/api/p2p/connect", json={"addr": addr}).json().get("results", [{}])[0]
            print(f"IPFS nodes connected: {'yes' if res.get('ok') else 'no (' + str(res.get('error')) + ')'}")
        else:
            print("IPFS is not running: the two clients share through the local store instead.")

        print()
        print("Two DOSR clients are running on this computer:")
        for dev in devices:
            print(f"  Laptop {dev.name.upper()}: http://127.0.0.1:{dev.gui_port}/   workspace {dev.workspace}")
        print("Create a repo on Laptop A; it shows up in Laptop B's inbox (P2P button, top right). "
              "Ctrl+C or close this window to stop both.")
        if not args.no_browser and not os.environ.get("DOSR_NO_BROWSER"):
            for dev in devices:
                webbrowser.open(f"http://127.0.0.1:{dev.gui_port}/")
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("A process exited; stopping the demo.")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        stop_all()


def cmd_reset(args):
    if WORKSPACE.exists():
        remove_workspace(DEVICE)
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

    store = ["--ipfs-api", IPFS_API, "--local-store", str(LOCAL_STORE)]
    base = ["--chain-state", str(CHAIN_STATE), "--attestor-url", attestor_url(args.attestor_port), *store]
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

        # 10. P2P: share over IPFS with a second device -------------------------------------------
        if not args.no_p2p:
            results.extend(p2p_scenario(args, console, scenario, dosr, store, as_client))

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


def p2p_scenario(args, console, scenario, dosr, store, as_client):
    """Device A (this workspace) shares calculator-py; device B (demo/workspace-b, its own
    mock chain and attestor) imports it, lands a change, and A pulls it back."""
    import json

    from rich.rule import Rule

    dev_b = Device("b")
    out = []
    if dev_b.workspace.exists():
        remove_workspace(dev_b)
    dev_b.workspace.mkdir(parents=True, exist_ok=True)
    over_ipfs = ipfs_up()
    scenario("10. Share over IPFS with a second device", "Alice → Bob on device B",
             ("IPFS node is running: share codes are real CIDs." if over_ipfs else
              "No IPFS node (run `demo.py ipfs install` + `demo.py up`): using the local store, which works the "
              "same on one computer.") + " Device B has its own workspace, mock chain and attestor "
             f"(:{dev_b.attestor_port}). Alice publishes; Bob imports the share code (B's client checks the bundle "
             "and replays every signed transition on B's chain), lands a change reviewed by B's attestor, and "
             "Alice pulls it back.")
    proc_b = None
    if not attestor_up(dev_b.attestor_port):
        proc_b = start_attestor_process(dev_b.attestor_port, dev_b)
    try:
        as_client("alice", "publish", repo="calculator-py")
        share = json.loads((WORKSPACE / "calculator-py" / ".git" / "dosr" / "p2p.json").read_text("utf-8"))[
            "published"][-1]["share"]
        console.print(Rule(f"[cyan]device B[/]: dosr import {share}", style="dim"))
        b_base = ["--chain-state", str(dev_b.chain_state), "--attestor-url", attestor_url(dev_b.attestor_port), *store]
        rc = dosr([*b_base, "--profile", str(CLIENTS / "bob.json"), "import", share,
                   "--workspace", str(dev_b.workspace)])
        out.append(("Device B: import calculator-py", "Bob", "imported", "imported" if rc == 0 else "error", None))

        with open(dev_b.workspace / "calculator-py" / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write("- Shared over P2P and improved on device B\n")
        console.print(Rule("[cyan]device B[/]: dosr submit (reviewed by device B's attestor)", style="dim"))
        rc = dosr([*b_base, "--profile", str(CLIENTS / "bob.json"), "-C", str(dev_b.workspace / "calculator-py"),
                   "submit", "-m", "Changelog entry from device B"])
        out.append(("Device B: PR on the shared repo", "Bob", "approved", "approved" if rc == 0 else f"exit {rc}", None))
        share_b = json.loads((dev_b.workspace / "calculator-py" / ".git" / "dosr" / "p2p.json").read_text("utf-8"))[
            "published"][-1]["share"]

        console.print(Rule(f"[cyan]device A[/]: dosr pull {share_b}", style="dim"))
        rc = as_client("alice", "pull", share_b, repo="calculator-py")
        out.append(("Device A: pull Bob's change", "Alice", "pulled", "pulled" if rc == 0 else "error", None))
    finally:
        if proc_b:
            proc_b.terminate()
    return out


def remove_workspace(device: Device) -> None:
    def retry_writable(func, path, _):  # git marks pack files read-only on Windows
        os.chmod(path, 0o700)
        func(path)
    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    shutil.rmtree(device.workspace, **{key: retry_writable})


def main():
    try:  # progress messages appear immediately even when output is piped (launchers, logs)
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    load_dotenv()
    os.environ["DOSR_TEMPLATES"] = os.pathsep.join(
        p for p in (os.environ.get("DOSR_TEMPLATES"), str(TEMPLATES)) if p)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="a",
                    help="which simulated device: a (default) or b, c, ... (own workspace, chain, ports)")
    ap.add_argument("--attestor-port", type=int, help="default: 8080 for device a, 8081 for b, ...")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("attestor", help="run the attestor in the foreground")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_attestor)

    for name, fn, helptext in (("gui", cmd_gui, "run the client GUI"),
                               ("up", cmd_up, "IPFS + attestor + GUI together")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--port", type=int, help="GUI port (default: 8765 for device a, 8766 for b, ...)")
        s.add_argument("--p2p-port", type=int, help="peer inbox port, reachable from the LAN "
                                                    "(default: 8790 for device a, 8791 for b, ...; 0 = off)")
        s.add_argument("--name", dest="device_name", help="device name other devices see (default: hostname, "
                                                          "or device-b etc. for simulated devices)")
        s.add_argument("--p2p-host", help="interface for the peer inbox (default: all, so other computers "
                                          "can reach it; 127.0.0.1 = this computer only)")
        s.add_argument("--no-browser", action="store_true")
        if name == "up":
            s.add_argument("--no-ipfs", action="store_true", help="do not start an IPFS node")
        s.set_defaults(func=fn)

    s = sub.add_parser("run", help="scripted end-to-end CLI demo")
    s.add_argument("--start-attestor", action="store_true", help="start an attestor if none is running")
    s.add_argument("--reset", action="store_true", help="wipe demo/workspace first")
    s.add_argument("--pause", action="store_true", help="wait for Enter between scenarios (for presenting)")
    s.add_argument("--no-p2p", action="store_true", help="skip scenario 10 (sharing with a second device)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("pair", help="two clients on this computer (devices a + b), peered, one tab each")
    s.add_argument("--no-ipfs", action="store_true", help="share through the local store instead of IPFS")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--devices", default="ab", help="which two simulated devices to run (default: ab)")
    s.set_defaults(func=cmd_pair)

    s = sub.add_parser("ipfs", help="IPFS node: install | start (default) | status")
    s.add_argument("action", nargs="?", choices=["start", "install", "status"], default="start")
    s.add_argument("--force", action="store_true", help="install: re-download even if present")
    s.set_defaults(func=cmd_ipfs)

    sub.add_parser("reset", help="delete this device's workspace").set_defaults(func=cmd_reset)

    args = ap.parse_args()
    global DEVICE, WORKSPACE, CHAIN_STATE
    DEVICE = Device(args.device)
    WORKSPACE, CHAIN_STATE = DEVICE.workspace, DEVICE.chain_state
    if args.attestor_port is None:
        args.attestor_port = DEVICE.attestor_port
    if getattr(args, "port", "absent") is None:
        args.port = DEVICE.attestor_port if args.cmd == "attestor" else DEVICE.gui_port
    if getattr(args, "p2p_port", "absent") is None:
        args.p2p_port = DEVICE.inbox_port
    if hasattr(args, "device_name") and not args.device_name:
        import socket
        args.device_name = socket.gethostname() if DEVICE.name == "a" else f"{socket.gethostname()}-{DEVICE.name}"
    if args.cmd == "attestor":
        args.attestor_port = args.port
    if args.cmd not in ("reset", "ipfs"):
        _require_deps()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
