"""Sharing DOSR repositories between devices over IPFS.

Publishing a repository puts two objects in the content store (ipfs.py):

1. a full Git bundle of the **canonical HEAD** (never unaccepted local commits), and
2. a small JSON *share manifest*: repository metadata, the canonical HEAD, the bundle's
   id and sha256, and the signed attestor response for every accepted transition from
   genesis to that HEAD.

The manifest's id (an IPFS CID) is the *share code* another device imports. Nothing in
it is trusted: on import the client checks the bundle digest, that the bundle's head is
the claimed canonical HEAD, that its root commit is the genesis and carries the claimed
policy, and replays every transition on the local mock chain, which verifies each
EIP-712 signature against the attestors in the genesis policy and parent == HEAD.

DEV STAND-IN FOR THE CHAIN. With a real contract, every device would read the
canonical HEAD from the chain and only fetch Git objects over IPFS. Until then each
device has its own mock chain, and importing a share code (1) registers the genesis on
the importer's chain the first time (trust on first use: the share code plays the role
of the contract address) and (2) moves its HEAD only through signature-checked
transitions. Two devices that both advance the same HEAD fork, exactly as two
transactions racing for the same parent would; the second import then reports a
conflict.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from dosr_chain import ChainError, MockChain
from dosr_git import GitError, GitRepo, create_bundle
from dosr_protocol.canonical import canonical_json, policy_hash, qualify_oid, sha256_0x, unqualify_oid

from .config import POLICY_FILE, REPO_FILE, ClientProfile, DosrRepo
from .ipfs import P2PStore, StoreError, is_cid, is_local_id

SHARE_TYPE = "dosr-share"
SHARE_VERSION = "0.1"
STATE_FILE = "p2p.json"  # in .git/dosr/
PEER_REF = "refs/dosr/peer-head"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_OID_RE = re.compile(r"^(sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_REPO_ID_RE = re.compile(r"^0x[0-9a-f]{64}$")


class P2PError(RuntimeError):
    pass


# ---------------------------------------------------------------- per-repo state


def load_state(repo: DosrRepo) -> dict:
    path = repo.state_dir / STATE_FILE
    state = {"transitions": [], "published": [], "imported": []}
    if path.exists():
        try:
            state.update(json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def _save_state(repo: DosrRepo, state: dict) -> None:
    path = repo.state_dir / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def attestation_hash(response: dict) -> str:
    return sha256_0x(canonical_json(response["attestation"]))


def _minimal(response: dict) -> dict:
    return {"attestation": response["attestation"], "signature": response["signature"]}


def accepted_transitions(repo: DosrRepo, onchain: dict) -> list[dict]:
    """The signed attestor response for each transition the local chain accepted, in
    order (genesis -> canonical HEAD). Found among this repo's own submissions and the
    transitions it imported from other devices."""
    known: dict[str, dict] = {}
    for t in load_state(repo)["transitions"]:
        known[attestation_hash(t)] = t
    for event_file in repo.submissions_dir.glob("*/chain-event.json"):
        try:
            resp = json.loads((event_file.parent / "response.json").read_text("utf-8"))
            known[attestation_hash(resp)] = _minimal(resp)
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    out = []
    for ev in onchain.get("events", []):
        t = known.get(ev["attestation_hash"])
        if not t:
            raise P2PError(f"no signed attestation found for accepted transition "
                           f"{ev['parent_git_oid'][:17]}… → {ev['candidate_git_oid'][:17]}… (block {ev['block']})")
        out.append(t)
    return out


# ---------------------------------------------------------------- publish


def publish(repo: DosrRepo, chain: MockChain, store: P2PStore, profile: ClientProfile | None = None,
            device: str | None = None) -> dict:
    """Publish the canonical HEAD + its signed history; returns the share code and manifest."""
    t0 = time.perf_counter()
    cfg, git = repo.config, repo.git
    onchain = chain.get_repository(cfg["repo_id"])
    if not onchain:
        raise P2PError("repository is not registered on the (mock) chain; nothing canonical to publish")
    head_q = onchain["canonical_head"]
    fmt, head = unqualify_oid(head_q)
    if not git.has_commit(head):
        raise P2PError(f"canonical HEAD {head[:10]} is not in the local repository")
    transitions = accepted_transitions(repo, onchain)

    backend = store.writer()
    with tempfile.TemporaryDirectory(prefix="dosr-publish-") as tmp:
        info = create_bundle(git, head, Path(tmp) / "repo.bundle", mode="full")
        bundle_bytes = Path(info.path).read_bytes()
    bundle_id = backend.add(bundle_bytes, f"{repo.name}.bundle")

    state = load_state(repo)
    previous = state["published"][-1]["share"] if state["published"] else None
    manifest = {
        "type": SHARE_TYPE,
        "version": SHARE_VERSION,
        "repo": cfg,
        "policy_hash": onchain["policy_hash"],
        "object_format": onchain.get("git_object_format", fmt),
        "genesis_git_oid": onchain["genesis_git_oid"],
        "canonical_head": head_q,
        "accepted_transitions": len(transitions),
        "transitions": transitions,
        "bundle": {"id": bundle_id, "sha256": info.sha256, "size_bytes": info.size_bytes,
                   "ref": info.head_ref},
        "published_by": f"{profile.name} <{profile.email}>" if profile else None,
        "device": device,
        "published_at": int(time.time()),
        "previous": previous,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    share = backend.add(manifest_bytes, f"{repo.name}.dosr-share.json")

    record = {"share": share, "canonical_head": head_q, "bundle": bundle_id, "backend": backend.kind,
              "accepted_transitions": len(transitions), "at": manifest["published_at"],
              "by": manifest["published_by"]}
    state["published"] = (state["published"] + [record])[-50:]
    _save_state(repo, state)
    return {
        "share": share,
        "backend": backend.kind,
        "canonical_head": head_q,
        "bundle": manifest["bundle"],
        "manifest_bytes": len(manifest_bytes),
        "accepted_transitions": len(transitions),
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------- fetch + verify


def parse_share_code(text: str) -> str:
    """Accept a bare id or anything containing one (e.g. a pasted ipfs:// or gateway URL)."""
    text = (text or "").strip()
    for token in re.split(r"[\s/?#]+", text):
        token = token.removeprefix("ipfs:")
        if is_cid(token) or is_local_id(token):
            return token
    m = re.search(r"local:[0-9a-f]{64}", text)
    if m:
        return m.group(0)
    raise P2PError("that does not look like a share code (an IPFS CID such as bafy…, or local:<sha256>)")


def fetch_manifest(store: P2PStore, share: str, timeout: float = 60) -> dict:
    raw = store.cat(share, timeout)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise P2PError("share manifest is too large")
    try:
        m = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise P2PError(f"{share[:16]}… is not a DOSR share manifest (not JSON); did you paste the bundle id?")
    if not isinstance(m, dict) or m.get("type") != SHARE_TYPE:
        raise P2PError(f"{share[:16]}… is not a DOSR share manifest")
    if m.get("version") != SHARE_VERSION:
        raise P2PError(f"share manifest version {m.get('version')!r} is not supported (client speaks {SHARE_VERSION})")
    repo = m.get("repo") or {}
    problems = []
    if not _REPO_ID_RE.match(str(repo.get("repo_id", ""))):
        problems.append("repo.repo_id")
    for key in ("canonical_head", "genesis_git_oid"):
        if not _OID_RE.match(str(m.get(key, ""))):
            problems.append(key)
    if not isinstance(m.get("transitions"), list):
        problems.append("transitions")
    b = m.get("bundle") or {}
    if not (is_cid(str(b.get("id", ""))) or is_local_id(str(b.get("id", "")))) or not str(b.get("sha256", "")).startswith("sha256:"):
        problems.append("bundle")
    if problems:
        raise P2PError("malformed share manifest: " + ", ".join(problems))
    return m


def _rmtree(path: Path) -> None:
    def retry_writable(func, p, _):  # git marks pack files read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        key = "onexc" if sys.version_info >= (3, 12) else "onerror"
        shutil.rmtree(path, **{key: retry_writable})


def find_repo(workspace: Path, repo_id: str) -> Path | None:
    if not workspace.is_dir():
        return None
    for d in sorted(workspace.iterdir()):
        if d.is_dir() and DosrRepo.is_dosr_repo(d):
            try:
                if json.loads((d / REPO_FILE).read_text("utf-8")).get("repo_id") == repo_id:
                    return d
            except (OSError, json.JSONDecodeError):
                continue
    return None


def _free_name(workspace: Path, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "repo"
    candidate, i = base, 2
    while (workspace / candidate).exists():
        candidate, i = f"{base}-{i}", i + 1
    return candidate


def _check_history(git: GitRepo, m: dict, head: str, fmt: str) -> dict:
    """The fetched history really is this repository: returns the genesis policy."""
    _, genesis = unqualify_oid(m["genesis_git_oid"])
    roots = git.run("rev-list", "--max-parents=0", head).split()
    if roots != [genesis]:
        raise P2PError(f"bundle history does not start at the claimed genesis {genesis[:10]}")
    try:
        policy = json.loads(git.run_bytes("show", f"{genesis}:{POLICY_FILE}"))
        repo_cfg = json.loads(git.run_bytes("show", f"{genesis}:{REPO_FILE}"))
    except (GitError, json.JSONDecodeError):
        raise P2PError("genesis commit has no valid .dosr/policy.json and .dosr/repo.json")
    if policy_hash(policy) != m["policy_hash"] or repo_cfg.get("policy_hash") != m["policy_hash"]:
        raise P2PError("genesis policy does not match the policy hash in the share manifest")
    if repo_cfg.get("repo_id") != m["repo"]["repo_id"]:
        raise P2PError("genesis .dosr/repo.json names a different repo_id than the share manifest")
    # .dosr/ is immutable after genesis (the attestor enforces it for every PR; re-check here).
    for path in (POLICY_FILE, REPO_FILE):
        if git.run("rev-parse", f"{genesis}:{path}").strip() != git.run("rev-parse", f"{head}:{path}").strip():
            raise P2PError(f"{path} changed after genesis")

    prev = m["genesis_git_oid"]
    for i, t in enumerate(m["transitions"]):
        att = (t or {}).get("attestation") or {}
        if att.get("repo_id") != m["repo"]["repo_id"]:
            raise P2PError(f"transition {i + 1} is for another repository")
        if att.get("parent_git_oid") != prev:
            raise P2PError(f"transition {i + 1} does not build on the previous one (history out of order)")
        cand = att.get("candidate_git_oid", "")
        cfmt, chex = unqualify_oid(cand)
        if cfmt != fmt or not git.has_commit(chex) or not git.is_ancestor(chex, head):
            raise P2PError(f"transition {i + 1} candidate {cand[:17]}… is not in the published history")
        prev = cand
    if prev != m["canonical_head"]:
        raise P2PError("the signed transitions do not end at the claimed canonical HEAD")
    return policy


def _forget(chain: MockChain, repo_id: str) -> None:
    """DEV ONLY: drop a repository's record from the mock chain (no real chain allows this)."""
    state = json.loads(chain.state_path.read_text("utf-8"))
    state["repos"].pop(repo_id, None)
    tmp = chain.state_path.with_suffix(".forget.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, chain.state_path)


def _apply_to_chain(chain: MockChain, git: GitRepo, m: dict, policy: dict, fmt: str, name: str,
                    replace: bool) -> tuple[bool, str | None, list[dict], str, list[str]]:
    """Register the genesis if needed and replay the share's signed transitions after the
    chain's current HEAD. Returns (registered, previous head, applied, status, warnings)."""
    repo_id, head_q = m["repo"]["repo_id"], m["canonical_head"]
    _, head = unqualify_oid(head_q)
    notes, status = [], "updated"
    onchain = chain.get_repository(repo_id)
    if onchain and (onchain["policy_hash"] != m["policy_hash"] or onchain["genesis_git_oid"] != m["genesis_git_oid"]):
        raise P2PError("your chain has a different genesis/policy registered for this repo id")
    before = onchain["canonical_head"] if onchain else None
    parents = [t["attestation"]["parent_git_oid"] for t in m["transitions"]]
    if onchain and before != head_q and before not in parents:
        bfmt, bhex = unqualify_oid(before)
        if git.has_commit(bhex) and git.is_ancestor(head, bhex):
            return False, before, [], "older", ["this share code is older than your canonical HEAD; nothing to apply"]
        if not replace:
            raise P2PError(f"conflict: your canonical HEAD {before[:17]}… is not in this share's history "
                           "(both devices accepted different changes on the same parent). To take theirs, "
                           "import again with 'adopt their history' (dev only)")
        _forget(chain, repo_id)
        onchain, status = None, "replaced"
        notes.append(f"DEV: discarded your chain's history for this repo (HEAD was {before[:17]}…) and adopted "
                     "the shared one")

    registered = False
    if not onchain:
        chain.register_repository(repo_id=repo_id, name=name, policy=policy,
                                  genesis_git_oid=m["genesis_git_oid"], object_format=fmt)
        onchain, registered = chain.get_repository(repo_id), status != "replaced"
    current = onchain["canonical_head"]
    if current == head_q:
        return registered, before, [], "up-to-date" if before == head_q else status, notes
    applied = []
    for tr in m["transitions"][parents.index(current):]:
        att = tr["attestation"]
        try:
            # Replayed as of its issue time: the contract accepted it then.
            ev = chain.accept_transition(tr, now=att["issued_at"])
        except ChainError as e:
            raise P2PError(f"transition {att['parent_git_oid'][:17]}… → {att['candidate_git_oid'][:17]}… "
                           f"failed verification: {e}")
        applied.append({"parent": ev["parent_git_oid"], "candidate": ev["candidate_git_oid"],
                        "block": ev["block"], "attestor": ev["attestor"],
                        "model": f"{att['provider']}/{att['model']}@{att['model_version']}"})
    return registered, before, applied, status, notes


def import_share(workspace, chain: MockChain, store: P2PStore, share: str, *, expect_head: str | None = None,
                 timeout: float = 60, replace: bool = False) -> dict:
    """Clone a shared repository into `workspace`, or update the copy already there.
    `replace` (dev only): on a conflict, drop this device's chain history for the repo and
    adopt the shared one instead of failing."""
    t0 = time.perf_counter()
    workspace = Path(workspace).resolve()
    share = parse_share_code(share)
    timings = {}

    t = time.perf_counter()
    m = fetch_manifest(store, share, timeout)
    timings["fetch_manifest_ms"] = round((time.perf_counter() - t) * 1000, 1)
    repo_id, head_q = m["repo"]["repo_id"], m["canonical_head"]
    if expect_head:
        want = expect_head.strip().lower().rpartition(":")[2]
        if len(want) < 7:
            raise P2PError("give at least 7 characters of the expected commit hash")
        if not unqualify_oid(head_q)[1].startswith(want):
            raise P2PError(f"share code is for HEAD {head_q[:17]}…, not the {expect_head!r} you expected")

    t = time.perf_counter()
    bundle = store.cat(m["bundle"]["id"], timeout)
    timings["fetch_bundle_ms"] = round((time.perf_counter() - t) * 1000, 1)
    if sha256_0x(bundle).replace("0x", "sha256:", 1) != m["bundle"]["sha256"]:
        raise P2PError("bundle digest does not match the share manifest (corrupted or substituted)")
    store.pin(share)
    store.pin(m["bundle"]["id"])

    existing = find_repo(workspace, repo_id)
    action = "pull" if existing else "clone"
    target = existing or workspace / _free_name(workspace, m["repo"].get("name") or "repo")
    warnings: list[str] = []
    created = False
    try:
        if action == "clone":
            workspace.mkdir(parents=True, exist_ok=True)
            git = GitRepo.init(target)
            created = True
        else:
            git = GitRepo(target)
        fmt = git.object_format()
        if m["object_format"] != fmt:
            raise P2PError(f"share is a {m['object_format']} repository; local git uses {fmt}")

        with tempfile.TemporaryDirectory(prefix="dosr-import-") as tmp:
            bpath = Path(tmp) / "repo.bundle"
            bpath.write_bytes(bundle)
            try:
                git.run("bundle", "verify", "-q", str(bpath))
                heads = git.run("bundle", "list-heads", str(bpath)).split()
            except GitError as e:
                raise P2PError(f"not a valid git bundle: {e.stderr}")
            if len(heads) < 2:
                raise P2PError("bundle has no refs")
            git.run("fetch", "-q", "--no-tags", str(bpath), f"+{heads[1]}:{PEER_REF}")
        _, head = unqualify_oid(head_q)
        if git.resolve(PEER_REF) != head:
            raise P2PError("bundle head is not the canonical HEAD the share manifest claims")
        policy = _check_history(git, m, head, fmt)

        # ---- chain (dev stand-in: see module docstring). Replayed on a scratch copy of the
        # chain first, so a share that fails half-way never leaves the real chain half-updated.
        name = m["repo"].get("name") or target.name
        with tempfile.TemporaryDirectory(prefix="dosr-chain-") as tmp:
            sandbox = MockChain(Path(tmp) / "state.json")
            if chain.state_path.exists():
                shutil.copyfile(chain.state_path, sandbox.state_path)
            _apply_to_chain(sandbox, git, m, policy, fmt, name, replace)
        registered, before, applied, status, notes = _apply_to_chain(chain, git, m, policy, fmt, name, replace)
        warnings += notes

        # ---- working copy
        now_head = chain.get_head(repo_id)
        _, now_hex = unqualify_oid(now_head)
        local = git.head()
        if status == "replaced" and local and not git.is_ancestor(local, now_hex):
            git.run("reset", "-q", "--hard", now_hex)
            warnings.append(f"your conflicting local commits were dropped from main (still in `git reflog`; "
                            f"old HEAD {local[:10]})")
            local = now_hex
        if action == "clone":
            git.update_ref("refs/heads/main", now_hex)
            git.run("reset", "-q", "--hard", "HEAD")
            working = f"checked out {now_hex[:10]} on main"
        elif local == now_hex:
            working = "working copy already at the canonical HEAD"
        elif local and git.is_ancestor(local, now_hex):
            try:
                git.run("merge", "-q", "--ff-only", now_hex)
                working = f"fast-forwarded {local[:10]} → {now_hex[:10]}"
            except GitError as e:
                working = "could not fast-forward the working copy"
                warnings.append(f"chain updated, but your uncommitted changes block the fast-forward ({e.stderr}); "
                                "commit or discard them, then pull again")
        elif local and git.is_ancestor(now_hex, local):
            working = f"local branch has {git.run('rev-list', '--count', f'{now_hex}..{local}').strip()} " \
                      "unaccepted commit(s) on top of the canonical HEAD"
        else:
            working = "local commits diverge from the canonical HEAD"
            warnings.append(f"your local commits do not build on the new canonical HEAD; rebase them: "
                            f"git rebase {now_hex[:10]}")
        git.delete_ref(PEER_REF)
    except Exception:
        if created:
            _rmtree(target)
        raise

    repo = DosrRepo(target)
    state = load_state(repo)
    have = {attestation_hash(t) for t in state["transitions"]}
    state["transitions"] += [_minimal(t) for t in m["transitions"] if attestation_hash(t) not in have]
    state["imported"] = (state["imported"] + [{
        "share": share, "canonical_head": head_q, "status": status, "applied": len(applied),
        "from": m.get("published_by"), "device": m.get("device"), "at": int(time.time())}])[-50:]
    _save_state(repo, state)
    return {
        "action": action,
        "status": status,
        "id": target.name,
        "name": repo.name,
        "path": str(target),
        "repo_id": repo_id,
        "share": share,
        "previous_head": None if registered else before,
        "canonical_head": now_head,
        "registered": registered,
        "applied": applied,
        "accepted_transitions": len(m["transitions"]),
        "working_copy": working,
        "published_by": m.get("published_by"),
        "device": m.get("device"),
        "published_at": m.get("published_at"),
        "bundle_bytes": len(bundle),
        "warnings": warnings,
        "timings": timings,
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


__all__ = ["P2PError", "StoreError", "publish", "import_share", "fetch_manifest", "parse_share_code",
           "accepted_transitions", "load_state", "find_repo"]
