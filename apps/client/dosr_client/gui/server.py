"""Local web GUI for the DOSR client.

A small FastAPI app that serves a single-page UI (static/) and a JSON API over
the same client library the CLI uses. It binds to 127.0.0.1 by default; there
is no authentication, so do not expose it on a network interface.

Submissions run in a background thread; the page polls /api/jobs/{id} to show
live step progress, latency, and ETA.

P2P (p2p.py, ipfs.py): repositories are published to IPFS (or a local store when no
IPFS node runs) and imported on other devices from a share code. With `p2p_port`, a
separate, minimal peer inbox (inbox.py) listens on the LAN so other devices can send
share codes here; the GUI API itself never leaves 127.0.0.1.
"""

import json
import os
import shutil
import socket
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dosr_chain import ChainError, MockChain
from dosr_git import GitError
from dosr_protocol.models import CATALOG, catalog_choices
from dosr_protocol.policy import PRESETS, build_policy, format_model, load_preset
from dosr_protocol.pr_format import TEMPLATES
from dosr_protocol.templates import TemplateError, to_toml

from .. import __version__
from ..attestor import AttestorClient, AttestorError
from ..config import (
    ClientProfile,
    ConfigError,
    DosrRepo,
    load_profile,
    profiles_in_dir,
    provider_api_key,
    provider_key_env,
    resolve_attestor_url,
    resolve_profile,
)
from ..pipeline import (
    BAD_REQUESTS,
    STEPS,
    SubmitOptions,
    Submission,
    approved_model_spec,
    model_choices,
    parse_model_choice,
)
from ..inbox import Inbox, Peers, announce_all, create_inbox_app, lan_ip, send_hello, serve_in_thread
from ..ipfs import IpfsStore, LocalStore, P2PStore, StoreError
from ..keys import KeyStore, KeyStoreError
from ..p2p import P2PError, find_repo, import_share, load_state, publish
from ..templates import (
    available_templates,
    check_text,
    policy_template,
    preview,
    resolve_template,
    scaffold,
    template_dirs,
)
from ..repo_ops import commit_all, create_repository, register_existing, repo_status

STATIC = Path(__file__).parent / "static"


def _provider_keys(providers) -> dict:
    """provider -> {env, set} for providers that need the contributor's API key."""
    return {p: {"env": provider_key_env(p), "set": bool(provider_api_key(p))}
            for p in sorted(providers) if provider_key_env(p)}
MAX_EDIT_BYTES = 512 * 1024


class GuiState:
    def __init__(self, workspace, seeds_dir, profiles_dir, profile_path, attestor_url, chain_state, env_file=None,
                 ipfs_api=None, local_store=None, p2p_mode="auto", device=None):
        self.keys = KeyStore(env_file)
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.p2p = None if p2p_mode == "off" else P2PStore(IpfsStore(ipfs_api), LocalStore(local_store),
                                                            mode=p2p_mode)
        p2p_dir = self.workspace / ".p2p"
        self.inbox = Inbox(p2p_dir / "inbox.json")
        self.peers = Peers(p2p_dir / "peers.json")
        self.device = device or os.environ.get("DOSR_DEVICE_NAME") or socket.gethostname()
        self.inbox_url: str | None = None  # set when the peer inbox is listening
        self.seeds_dir = Path(seeds_dir).resolve() if seeds_dir else None
        self.profiles_dir = Path(profiles_dir).resolve() if profiles_dir else None
        self.explicit_attestor_url = attestor_url
        self.chain = MockChain(chain_state) if chain_state else MockChain()
        self.jobs: dict[str, Submission] = {}
        self.lock = threading.Lock()

        self.profiles: dict[str, ClientProfile] = {p.id: p for p in profiles_in_dir(self.profiles_dir)}
        if profile_path:
            p = load_profile(profile_path)
            self.profiles.setdefault(p.id, p)
            self.active_profile_id = p.id
        elif self.profiles:
            self.active_profile_id = next(iter(self.profiles))
        else:
            p = resolve_profile(None)
            self.profiles[p.id] = p
            self.active_profile_id = p.id

    @property
    def profile(self) -> ClientProfile:
        return self.profiles[self.active_profile_id]

    def attestor(self) -> AttestorClient:
        return AttestorClient(resolve_attestor_url(self.explicit_attestor_url, self.profile), timeout=120)

    def repo(self, name: str) -> DosrRepo:
        path = (self.workspace / name).resolve()
        if path.parent != self.workspace or not DosrRepo.is_dosr_repo(path):
            raise HTTPException(404, f"no DOSR repository named {name!r} in the workspace")
        return DosrRepo(path)

    def store(self) -> P2PStore:
        if self.p2p is None:
            raise HTTPException(400, "P2P is off (started with --p2p off)")
        return self.p2p

    def hello(self) -> dict:
        """What other devices learn about this one (peer inbox /dosr/hello)."""
        out = {"device": self.device, "inbox_url": self.inbox_url, "peer_id": None, "ipfs_addrs": []}
        if self.p2p is not None and self.p2p.ipfs_up():
            try:
                info = self.p2p.ipfs.info()
                out.update(peer_id=info["peer_id"],
                           ipfs_addrs=[a if "/p2p/" in a else f"{a}/p2p/{info['peer_id']}"
                                       for a in info["shareable_addresses"]][:20])
            except StoreError:
                pass
        return out

    def publish_and_announce(self, repo: DosrRepo) -> dict:
        pub = publish(repo, self.chain, self.store(), self.profile, device=self.device)
        me = self.hello()
        pub["announced"] = announce_all(self.peers, {
            "share": pub["share"], "repo_id": repo.config["repo_id"], "name": repo.name,
            "canonical_head": pub["canonical_head"], "accepted_transitions": pub["accepted_transitions"],
            "from_name": f"{self.profile.name} <{self.profile.email}>", "device": self.device,
            "inbox_url": self.inbox_url, "ipfs_addrs": me["ipfs_addrs"],
        })
        return pub

    def seeds(self) -> list[dict]:
        if not self.seeds_dir or not self.seeds_dir.is_dir():
            return []
        out = []
        for d in sorted(self.seeds_dir.iterdir()):
            meta_file = d / "seed.json"
            if not meta_file.exists():
                continue
            meta = json.loads(meta_file.read_text("utf-8"))
            out.append({"id": d.name, **{k: meta.get(k) for k in ("name", "description", "preset", "language")},
                        "policy_overrides": meta.get("policy_overrides", {})})
        return out


def _safe_path(repo: DosrRepo, rel: str) -> Path:
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith(".git/") or rel == ".git" or "/.git/" in f"/{rel}/":
        raise HTTPException(400, "invalid path")
    p = (repo.path / rel).resolve()
    if repo.path not in p.parents:
        raise HTTPException(400, "path escapes the repository")
    return p


# ---------------------------------------------------------------- request bodies


class TemplateTextBody(BaseModel):
    text: str
    overwrite: bool = False


class KeyBody(BaseModel):
    key: str


class ProfileBody(BaseModel):
    id: str


class CreateRepoBody(BaseModel):
    name: str
    description: str = ""
    preset: str = "standard"
    seed: str | None = None
    approved_models: list[str] | None = None
    prompt_template: str | None = None
    max_changed_files: int | None = None
    max_patch_bytes: int | None = None
    minimum_approvals: int | None = None
    context_files: list[str] | None = None


class FileBody(BaseModel):
    path: str
    content: str


class CommitBody(BaseModel):
    message: str


class ImportBody(BaseModel):
    share: str
    expect_head: str | None = None
    inbox_id: str | None = None  # the inbox item this came from (its sender's IPFS addresses are dialed first)
    timeout: float = 60
    adopt: bool = False  # dev: on a conflict, adopt the shared history (p2p.import_share replace=True)


class PeerBody(BaseModel):
    url: str


class ConnectBody(BaseModel):
    addr: str


class SubmitBody(BaseModel):
    message: str | None = None
    model: str | None = None
    register_on_chain: bool = True
    dry_run: bool = False
    bad_request: str | None = None
    force: bool = False


# ---------------------------------------------------------------- app


def create_app(state: GuiState) -> FastAPI:
    app = FastAPI(title="DOSR Client GUI", version=__version__)

    @app.exception_handler(ConfigError)
    @app.exception_handler(P2PError)
    @app.exception_handler(StoreError)
    @app.exception_handler(ValueError)
    @app.exception_handler(GitError)
    @app.exception_handler(ChainError)
    async def _user_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    # ---- global state

    @app.get("/api/state")
    def get_state():
        repos = []
        for d in sorted(state.workspace.iterdir()):
            if d.is_dir() and DosrRepo.is_dosr_repo(d):
                try:
                    r = DosrRepo(d)
                    onchain = state.chain.get_repository(r.config["repo_id"])
                    repos.append({"id": d.name, "name": r.name, "registered": bool(onchain),
                                  "description": r.config.get("description", ""),
                                  "transitions": onchain["accepted_transition_count"] if onchain else 0})
                except Exception as e:
                    repos.append({"id": d.name, "name": d.name, "error": str(e)})
        return {
            "version": __version__,
            "workspace": str(state.workspace),
            "chain_state": str(state.chain.state_path),
            "attestor_url": state.attestor().base_url,
            "profiles": [p.to_dict() for p in state.profiles.values()],
            "active_profile": state.active_profile_id,
            "repos": repos,
            "seeds": state.seeds(),
            "presets": [{"id": k, "description": v, "policy": load_preset(k)} for k, v in PRESETS.items()],
            "templates": available_templates(),
            "templates_dir": str(template_dirs()[0]),
            "steps": [{"id": i, "label": label} for i, label in STEPS],
            "device": state.device,
            "bad_requests": [{"id": k, "description": v} for k, v in BAD_REQUESTS.items()],
            "model_catalog": {
                "providers": [{"id": k, "label": v["label"]} for k, v in CATALOG.items()],
                "models": catalog_choices(),
                "keys": _provider_keys(CATALOG),
            },
        }

    # ---- PR templates (files: dosr_protocol/templates.py)

    def _template_view(t, text=None, path=None):
        return {"id": t.id, "description": t.description, "builtin": t.builtin, "hash": t.hash, "path": path,
                "toml": text if text is not None else to_toml(t), "preview": preview(t),
                "sections": [s["type"] for s in t.sections]}

    @app.get("/api/templates")
    def list_templates():
        return {"templates": available_templates(), "dir": str(template_dirs()[0])}

    @app.get("/api/templates/{template_id}")
    def get_template_file(template_id: str):
        if template_id in TEMPLATES:
            return _template_view(TEMPLATES[template_id])
        try:
            t, _ = resolve_template(template_id)
        except TemplateError as e:
            raise HTTPException(404, str(e))
        path = next((r["path"] for r in available_templates() if r["id"] == t.id), None)
        text = Path(path).read_text("utf-8") if path else None
        return _template_view(t, text, path)

    @app.get("/api/template-scaffold")
    def template_scaffold(id: str, base: str = "standard-code-review-v1"):
        try:
            base_t = TEMPLATES[base] if base in TEMPLATES else resolve_template(base)[0]
            return {"toml": scaffold(id, base_t)}
        except TemplateError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/templates/check")
    def check_template(body: TemplateTextBody):
        try:
            t = check_text(body.text)
            if t.id in TEMPLATES:
                raise TemplateError(f"id {t.id!r} is a built-in template; give your template its own id")
        except TemplateError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **_template_view(t, body.text)}

    @app.post("/api/templates")
    def save_template(body: TemplateTextBody):
        try:
            t = check_text(body.text)
            if t.id in TEMPLATES:
                raise TemplateError(f"id {t.id!r} is a built-in template; give your template its own id")
        except TemplateError as e:
            raise HTTPException(400, str(e))
        directory = template_dirs()[0]
        path = directory / f"{t.id}.toml"  # the id is [a-z0-9._-], so this stays in `directory`
        if path.exists() and path.read_text("utf-8") != body.text and not body.overwrite:
            raise HTTPException(409, f"{path.name} already exists; save with overwrite to replace it "
                                     "(repositories that already use it keep their embedded copy)")
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(body.text, encoding="utf-8")
        return {"saved": str(path), **_template_view(t, body.text, str(path))}

    @app.get("/api/repos/{name}/template")
    def repo_template(name: str):
        repo = state.repo(name)
        try:
            t = policy_template(repo.policy)
        except Exception as e:
            raise HTTPException(400, str(e))
        return _template_view(t)

    # ---- contributor API keys (never returned in full)

    @app.get("/api/keys")
    def get_keys():
        return {"keys": state.keys.status(),
                "env_file": str(state.keys.env_file) if state.keys.env_file else None}

    @app.put("/api/keys/{provider}")
    def put_key(provider: str, body: KeyBody):
        try:
            return state.keys.set(provider, body.key)
        except KeyStoreError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/keys/{provider}")
    def delete_key(provider: str):
        try:
            return state.keys.remove(provider)
        except KeyStoreError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/profile")
    def set_profile(body: ProfileBody):
        if body.id not in state.profiles:
            raise HTTPException(404, f"unknown profile {body.id}")
        state.active_profile_id = body.id
        return {"active_profile": body.id}

    @app.get("/api/attestor")
    def attestor_health():
        client = state.attestor()
        t0 = time.perf_counter()
        try:
            health = client.health()
            keys = client.keys()["keys"]
            return {"up": True, "url": client.base_url, "health": health, "keys": keys,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
        except AttestorError as e:
            return {"up": False, "url": client.base_url, "error": str(e)}

    # ---- repositories

    @app.post("/api/repos")
    def create_repo(body: CreateRepoBody):
        name = body.name.strip()
        if not name or any(c in name for c in '/\\:*?"<>|') or name.startswith("."):
            raise HTTPException(400, "repository name must be a simple folder name")
        path = state.workspace / name
        if path.exists():
            raise HTTPException(409, f"{name} already exists in the workspace")
        seed_dir = None
        if body.seed:
            seed = next((s for s in state.seeds() if s["id"] == body.seed), None)
            if not seed:
                raise HTTPException(404, f"unknown seed {body.seed}")
            seed_dir = state.seeds_dir / body.seed / "files"
        template_id, template_def = None, None
        if body.prompt_template:
            try:
                t, template_def = resolve_template(body.prompt_template)
            except TemplateError as e:
                raise HTTPException(400, str(e))
            template_id = t.id
        policy = build_policy(
            body.preset,
            repo_name=name,
            approved_models=[approved_model_spec(m) for m in body.approved_models] if body.approved_models else None,
            prompt_template=template_id,
            template=template_def,
            max_changed_files=body.max_changed_files,
            max_patch_bytes=body.max_patch_bytes,
            minimum_approvals=body.minimum_approvals,
            context_files=[g.strip() for g in body.context_files if g.strip()] if body.context_files else None,
        )
        t0 = time.perf_counter()
        try:
            created = create_repository(path, name=name, description=body.description, policy=policy,
                                        profile=state.profile, chain=state.chain, seed_dir=seed_dir)
        except Exception:
            if path.exists() and not (path / ".dosr").exists():
                shutil.rmtree(path, ignore_errors=True)
            raise
        created["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        created["id"] = name
        if state.p2p is not None and created.get("registration"):
            try:  # best effort: makes the new repo importable right away
                created["published"] = state.publish_and_announce(DosrRepo(path))
            except Exception as e:
                created["publish_error"] = str(e)
        return created

    @app.get("/api/repos/{name}")
    def get_repo(name: str):
        repo = state.repo(name)
        st = repo_status(repo, state.chain)
        st["id"] = name
        if st.get("policy"):
            approved = st["policy"]["review"]["approved_models"]
            st["approved_models"] = [format_model(m) for m in approved]
            # Concrete versions to offer (ranges expanded), then other common models.
            st["model_choices"] = model_choices(st["policy"])
            st["provider_keys"] = _provider_keys({c["provider"] for c in st["model_choices"]})
            try:
                t = policy_template(st["policy"])
                st["template"] = {"id": t.id, "builtin": t.builtin, "hash": t.hash, "description": t.description}
            except Exception as e:
                st["template"] = {"id": st["policy"]["review"]["prompt_template"], "error": str(e)}
        st["repo_config"] = repo.config
        return st

    @app.post("/api/repos/{name}/register")
    def register(name: str):
        return register_existing(state.repo(name), state.chain)

    @app.get("/api/repos/{name}/files")
    def list_files(name: str):
        repo = state.repo(name)
        changes = {c["path"]: c["code"] for c in repo.git.status()}
        files = [{"path": p, "status": changes.get(p)} for p in repo.git.worktree_files()]
        deleted = [{"path": p, "status": c} for p, c in changes.items() if "D" in c]
        return {"files": files + deleted}

    @app.get("/api/repos/{name}/file")
    def read_file(name: str, path: str):
        p = _safe_path(state.repo(name), path)
        if not p.is_file():
            raise HTTPException(404, "file not found")
        data = p.read_bytes()
        if len(data) > MAX_EDIT_BYTES or b"\0" in data[:8000]:
            return {"path": path, "binary": True, "content": None, "size": len(data)}
        return {"path": path, "binary": False, "content": data.decode("utf-8", errors="replace"),
                "size": len(data), "readonly": path.startswith(".dosr/")}

    @app.put("/api/repos/{name}/file")
    def write_file(name: str, body: FileBody):
        repo = state.repo(name)
        p = _safe_path(repo, body.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the file's existing line-ending style (textarea always gives \n).
        content = body.content
        if p.exists() and b"\r\n" in p.read_bytes()[:8000]:
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_bytes(content.encode("utf-8"))
        return {"saved": body.path, "size": p.stat().st_size}

    @app.delete("/api/repos/{name}/file")
    def delete_file(name: str, path: str):
        p = _safe_path(state.repo(name), path)
        if not p.is_file():
            raise HTTPException(404, "file not found")
        p.unlink()
        return {"deleted": path}

    @app.get("/api/repos/{name}/diff")
    def diff(name: str):
        repo = state.repo(name)
        try:
            base = state.chain.get_head(repo.config["repo_id"]).partition(":")[2]
        except Exception:
            base = repo.git.head()
        return {"base": base, "diff": repo.git.diff_worktree(base)}

    @app.post("/api/repos/{name}/commit")
    def commit(name: str, body: CommitBody):
        oid = commit_all(state.repo(name), body.message, state.profile)
        return {"commit": oid}

    @app.post("/api/repos/{name}/discard")
    def discard(name: str):
        """Throw away uncommitted changes (demo convenience)."""
        repo = state.repo(name)
        repo.git.run("reset", "-q", "--hard", "HEAD")
        repo.git.run("clean", "-q", "-fd")
        return {"ok": True}

    # ---- submissions

    @app.post("/api/repos/{name}/submit")
    def start_submit(name: str, body: SubmitBody):
        repo = state.repo(name)
        if body.bad_request and body.bad_request not in BAD_REQUESTS:
            raise HTTPException(400, f"unknown bad_request {body.bad_request!r}")
        with state.lock:
            busy = [j for j in state.jobs.values() if j.repo.path == repo.path and j.outcome in ("pending", "running")]
            if busy:
                raise HTTPException(409, "a submission for this repository is already running")
            opts = SubmitOptions(
                message=body.message or None,
                model=parse_model_choice(body.model) if body.model else None,
                register=body.register_on_chain,
                dry_run=body.dry_run,
                bad_request=body.bad_request or None,
                force=body.force,
            )
            publisher = state.publish_and_announce if state.p2p is not None else None
            sub = Submission(repo, state.profile, state.attestor(), state.chain, opts, publisher)
            state.jobs[sub.request_id] = sub
        threading.Thread(target=sub.run, name=f"submit-{sub.request_id}", daemon=True).start()
        return {"job_id": sub.request_id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        sub = state.jobs.get(job_id)
        if not sub:
            raise HTTPException(404, "unknown job")
        return sub.to_dict()

    @app.get("/api/repos/{name}/submissions")
    def submissions(name: str):
        return {"submissions": state.repo(name).list_submissions()}

    @app.get("/api/repos/{name}/submissions/{request_id}")
    def submission(name: str, request_id: str):
        return state.repo(name).load_submission(request_id)

    @app.get("/api/chain")
    def chain():
        return {"state_path": str(state.chain.state_path), "repos": state.chain.list_repositories()}

    # ---- P2P: publish / import over IPFS, peers, inbox

    @app.get("/api/p2p")
    def p2p_status():
        out = {"enabled": state.p2p is not None, "device": state.device, "inbox_url": state.inbox_url,
               "peers": state.peers.list(), "inbox_new": sum(1 for i in state.inbox.list() if i["status"] == "new")}
        if state.p2p is not None:
            out.update(state.p2p.status())
            if out.get("ipfs"):
                out["ipfs_addrs"] = state.hello()["ipfs_addrs"]
        return out

    @app.post("/api/repos/{name}/publish")
    def publish_repo(name: str):
        return state.publish_and_announce(state.repo(name))

    @app.get("/api/repos/{name}/p2p")
    def repo_p2p(name: str):
        st = load_state(state.repo(name))
        return {"published": st["published"][::-1], "imported": st["imported"][::-1],
                "last": st["published"][-1] if st["published"] else None}

    @app.post("/api/p2p/import")
    def p2p_import(body: ImportBody):
        store = state.store()
        dialed = []
        item = state.inbox.get(body.inbox_id) if body.inbox_id else None
        if item and item.get("ipfs_addrs") and store.ipfs_up():
            dialed = store.ipfs.connect(item["ipfs_addrs"][:6])
        res = import_share(state.workspace, state.chain, store, body.share, expect_head=body.expect_head,
                           timeout=body.timeout, replace=body.adopt)
        res["dialed"] = dialed
        if item:
            state.inbox.set_status(item["id"], "pulled")
        return res

    @app.get("/api/p2p/inbox")
    def p2p_inbox():
        items = state.inbox.list()
        for i in items:  # what pulling it would do here
            local = find_repo(state.workspace, i["repo_id"])
            i["local_repo"] = local.name if local else None
            if local:
                try:
                    i["local_head"] = state.chain.get_head(i["repo_id"])
                except ChainError:
                    i["local_head"] = None
        return {"items": items}

    @app.post("/api/p2p/inbox/{item_id}/dismiss")
    def p2p_dismiss(item_id: str):
        if not state.inbox.set_status(item_id, "dismissed"):
            raise HTTPException(404, "unknown inbox item")
        return {"ok": True}

    @app.post("/api/p2p/peers")
    def add_peer(body: PeerBody):
        """Introduce ourselves to another device's inbox (it adds us back) and dial its IPFS node."""
        try:
            hello = send_hello(body.url, state.hello())
        except Exception as e:
            raise HTTPException(400, f"could not reach a DOSR peer inbox at {body.url}: {e}. Check the address "
                                     "(the other GUI shows it under P2P), that both are on the same network, and "
                                     "that its firewall allows the port")
        dialed = []
        if hello.get("ipfs_addrs") and state.p2p is not None and state.p2p.ipfs_up():
            dialed = state.p2p.ipfs.connect(hello["ipfs_addrs"][:6])
        peer = state.peers.upsert(body.url, device=hello.get("device"), peer_id=hello.get("peer_id"),
                                  ipfs_addrs=hello.get("ipfs_addrs"), last_seen=int(time.time()))
        return {"peer": peer, "dialed": dialed}

    @app.delete("/api/p2p/peers")
    def remove_peer(url: str):
        state.peers.remove(url)
        return {"ok": True}

    @app.post("/api/p2p/connect")
    def p2p_connect(body: ConnectBody):
        store = state.store()
        if not store.ipfs_up():
            raise HTTPException(400, "IPFS is not running on this device")
        return {"results": store.ipfs.connect([body.addr.strip()])}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(*, host="127.0.0.1", port=8765, workspace=".", seeds_dir=None, profiles_dir=None,
          profile_path=None, attestor_url=None, chain_state=None, open_browser=True, env_file=None,
          ipfs_api=None, local_store=None, p2p_mode="auto", p2p_port=None, p2p_host="0.0.0.0", device=None):
    import uvicorn

    state = GuiState(workspace, seeds_dir, profiles_dir, profile_path, attestor_url, chain_state, env_file,
                     ipfs_api=ipfs_api, local_store=local_store, p2p_mode=p2p_mode, device=device)
    app = create_app(state)
    if p2p_port and state.p2p is not None:
        serve_in_thread(create_inbox_app(state.inbox, state.peers, state.hello), p2p_host, p2p_port)
        shown = lan_ip() if p2p_host in ("0.0.0.0", "::") else p2p_host
        state.inbox_url = f"http://{shown}:{p2p_port}"
    url = f"http://{host}:{port}/"
    print(f"DOSR client GUI on {url}")
    print(f"  workspace : {state.workspace}")
    print(f"  attestor  : {state.attestor().base_url}")
    print(f"  chain     : {state.chain.state_path}")
    print(f"  API keys  : {state.keys.env_file or 'this session only (no --env-file)'}")
    if state.p2p is not None:
        st = state.p2p.status()
        print(f"  P2P store : {'IPFS ' + st['ipfs_api'] if st['backend'] == 'ipfs' else 'local store ' + st['local_store'] + ' (IPFS not running)'}")
    if state.inbox_url:
        print(f"  peer inbox: {state.inbox_url}  (give this address to the other device)")
    if open_browser and not os.environ.get("DOSR_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
