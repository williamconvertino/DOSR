"""The chain as the source of truth for a repository's state.

`verify(repo, chain)` checks a local clone against what the chain records, using only
the chain and local Git objects:

- the chain holds this repository with the genesis commit and policy hash in .dosr/,
  and the attestor keys it registered are exactly the ones the policy lists;
- the canonical history is a linear chain of transitions genesis -> ... -> HEAD, every
  step of which is in the local Git history (each candidate descends from its parent);
- each transition's signed attestation (rebuilt from the transaction calldata on an EVM
  chain) recovers to a registered attestor and hashes to the digest the contract stored;
- where the local branch stands: in sync, ahead (unaccepted local commits), behind,
  or diverged from the canonical HEAD.

`sync(repo, chain, store)` brings a clone up to the canonical HEAD. Git objects come
from the snapshot the chain points at (an IPFS share manifest, verified on import, see
p2p.py); unaccepted local commits are rebased onto the new HEAD. This is how a client
recovers after losing a race: its approved PR was built on a parent that is no longer
HEAD, so it syncs, rebases, and submits again (the attestor must review the new commit).
"""

import time
from pathlib import Path

from dosr_git import GitError
from dosr_protocol.canonical import unqualify_oid
from dosr_protocol.eip712 import attestation_digest, recover_signer

from .config import POLICY_FILE, ClientProfile, DosrRepo


class SyncError(RuntimeError):
    pass


def _check(checks, name, ok, detail="", fix=None):
    checks.append({"name": name, "ok": ok, "detail": detail, **({"fix": fix} if fix else {})})
    return ok


def _signed_history(repo: DosrRepo, chain, onchain: dict) -> list[dict | None]:
    """The signed attestation for each on-chain transition: from the chain itself when it
    keeps calldata (EVM), else from this clone's submissions / imported share manifests."""
    if hasattr(chain, "signed_transitions"):
        try:
            return chain.signed_transitions(onchain["repo_id"])
        except Exception:
            pass
    from .p2p import known_transitions

    known = known_transitions(repo)
    return [known.get(ev.get("attestation_digest")) for ev in onchain.get("events", [])]


def relation(git, canonical_hex: str) -> dict:
    head = git.head()
    out = {"local_head": head, "state": "unknown", "ahead": 0, "behind": 0}
    if not head:
        out["state"] = "empty"
    elif not git.has_commit(canonical_hex):
        out["state"] = "missing"  # the chain moved to a commit we have not fetched
    elif head == canonical_hex:
        out["state"] = "in-sync"
    elif git.is_ancestor(canonical_hex, head):
        out["state"] = "ahead"
        out["ahead"] = int(git.run("rev-list", "--count", f"{canonical_hex}..{head}").strip())
    elif git.is_ancestor(head, canonical_hex):
        out["state"] = "behind"
        out["behind"] = int(git.run("rev-list", "--count", f"{head}..{canonical_hex}").strip())
    else:
        out["state"] = "diverged"
        out["ahead"] = int(git.run("rev-list", "--count", f"{canonical_hex}..{head}").strip())
        out["behind"] = int(git.run("rev-list", "--count", f"{head}..{canonical_hex}").strip())
    return out


def verify(repo: DosrRepo, chain) -> dict:
    """Check this clone against the chain. Returns {status, checks, history, relation, ...}."""
    t0 = time.perf_counter()
    cfg, git = repo.config, repo.git
    checks: list[dict] = []
    out = {"repo": repo.name, "repo_id": cfg["repo_id"], "chain": chain.kind, "checks": checks, "history": [],
           "relation": None, "status": "error", "canonical_head": None}

    try:
        onchain = chain.get_repository(cfg["repo_id"])
    except Exception as e:
        _check(checks, "chain_reachable", False, str(e))
        return out
    _check(checks, "chain_reachable", True, chain.location)
    if not _check(checks, "repository_registered", onchain is not None,
                  "" if onchain else "the chain has no record of this repository id",
                  fix=None if onchain else "register"):
        out["status"] = "not-registered"
        return out
    out["canonical_head"] = onchain["canonical_head"]
    out["snapshot"] = onchain.get("snapshot")

    # ---- genesis + policy
    try:
        local_policy_hash = repo.computed_policy_hash()
    except Exception as e:
        local_policy_hash = f"invalid ({e})"
    _check(checks, "policy_hash_matches", onchain["policy_hash"] == local_policy_hash == cfg["policy_hash"],
           f"chain {onchain['policy_hash'][:18]}… · .dosr/policy.json {str(local_policy_hash)[:18]}… · "
           f".dosr/repo.json {cfg['policy_hash'][:18]}…")
    roots = git.run("rev-list", "--max-parents=0", "HEAD", check=False).split() if git.head() else []
    _, genesis_hex = unqualify_oid(onchain["genesis_git_oid"])
    _check(checks, "genesis_matches", roots == [genesis_hex],
           f"chain genesis {genesis_hex[:10]}, local root {', '.join(r[:10] for r in roots) or 'none'}")
    try:
        listed = {(a["key_id"], a["address"].lower()) for a in repo.policy.get("attestors", [])}
    except Exception:
        listed = set()
    registered = {(a["key_id"], a["address"].lower()) for a in onchain.get("attestors", [])}
    if registered or chain.kind == "evm":
        _check(checks, "attestors_match_policy", listed == registered,
               f"{len(registered)} registered on-chain, {len(listed)} in the policy"
               + ("" if listed == registered else ": the chain trusts a different attestor set than the policy"))

    # ---- canonical history
    events = onchain.get("events", [])
    signed = _signed_history(repo, chain, onchain)
    signers = {a["key_id"]: a["address"].lower() for a in onchain.get("attestors", [])}
    prev, linear, have_all, sig_ok, sig_missing = onchain["genesis_git_oid"], True, True, True, 0
    history = [{"seq": 0, "oid": onchain["genesis_git_oid"], "kind": "genesis", "block": onchain.get("registered_block"),
                "local": git.has_commit(genesis_hex)}]
    for i, ev in enumerate(events):
        row = {"seq": i + 1, "oid": ev["candidate_git_oid"], "parent": ev["parent_git_oid"], "kind": "transition",
               "block": ev.get("block"), "tx_hash": ev.get("tx_hash"), "attestor": ev.get("attestor"),
               "submitter": ev.get("submitter"), "digest": ev.get("attestation_digest"),
               "timestamp": ev.get("timestamp")}
        if ev["parent_git_oid"] != prev:
            linear = False
        _, chex = unqualify_oid(ev["candidate_git_oid"])
        _, phex = unqualify_oid(ev["parent_git_oid"])
        row["local"] = git.has_commit(chex)
        if row["local"] and git.has_commit(phex) and not git.is_ancestor(phex, chex):
            linear = False
            row["problem"] = "candidate does not descend from its parent"
        have_all &= row["local"]
        s = signed[i] if i < len(signed) else None
        if s:
            att = s["attestation"]
            row["model"] = f"{att.get('provider')}/{att.get('model')}@{att.get('model_version')}"
            row["attestor_key_id"] = att.get("attestor_key_id")
            try:
                ok = (recover_signer(att, s["signature"]["value"]).lower() == signers.get(att.get("attestor_key_id"))
                      and attestation_digest(att) == ev.get("attestation_digest")
                      and att.get("approved") is True
                      and att.get("parent_git_oid") == ev["parent_git_oid"]
                      and att.get("candidate_git_oid") == ev["candidate_git_oid"])
            except Exception:
                ok = False
            row["signature_ok"] = ok
            sig_ok &= ok
        else:
            row["signature_ok"] = None
            sig_missing += 1
        history.append(row)
        prev = ev["candidate_git_oid"]
    out["history"] = history
    _check(checks, "history_is_linear", linear and prev == onchain["canonical_head"],
           f"genesis → {len(events)} transition(s) → HEAD {onchain['canonical_head'][:17]}…")
    if sig_missing == len(events) and events:
        _check(checks, "signatures_valid", None, "no signed attestations available to re-check (mock chain without "
                                                 "local copies)")
    else:
        _check(checks, "signatures_valid", sig_ok,
               f"{len(events) - sig_missing} attestation(s) re-verified against the registered attestors"
               + (f", {sig_missing} unavailable" if sig_missing else "")
               + ("" if sig_ok else ": a signature or digest does not match the chain"))
    _check(checks, "canonical_objects_present", have_all,
           "every canonical commit is in the local repository" if have_all else
           f"missing {sum(1 for h in history if not h['local'])} canonical commit(s): sync to fetch them",
           fix=None if have_all else "sync")

    # ---- the local branch vs HEAD
    _, canon_hex = unqualify_oid(onchain["canonical_head"])
    rel = relation(git, canon_hex)
    out["relation"] = rel
    detail = {
        "in-sync": "local main is the canonical HEAD",
        "ahead": f"{rel['ahead']} local commit(s) on top of the canonical HEAD, not yet accepted (submit them)",
        "behind": f"the chain is {rel['behind']} commit(s) ahead of local main (sync to fast-forward)",
        "diverged": f"local main has {rel['ahead']} commit(s) that do not build on the canonical HEAD "
                    "(another PR landed first): sync to rebase them",
        "missing": "the canonical HEAD is not in the local repository yet (sync to fetch it)",
        "empty": "no local commits",
    }.get(rel["state"], rel["state"])
    _check(checks, "local_branch", rel["state"] in ("in-sync", "ahead"), detail,
           fix="sync" if rel["state"] in ("behind", "diverged", "missing") else None)
    if git.head():
        for path in (POLICY_FILE, ".dosr/repo.json"):
            same = git.run("rev-parse", f"{genesis_hex}:{path}", check=False).strip() == \
                git.run("rev-parse", f"HEAD:{path}", check=False).strip()
            if not same:
                _check(checks, "dosr_files_unchanged", False, f"{path} differs from genesis")
                break
        else:
            _check(checks, "dosr_files_unchanged", True, ".dosr/ is identical to genesis")

    failed = [c for c in checks if c["ok"] is False]
    integrity = [c for c in failed if c["name"] not in ("local_branch", "canonical_objects_present")]
    out["status"] = "invalid" if integrity else (rel["state"] if rel["state"] != "missing" else "behind")
    out["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def sync(repo: DosrRepo, chain, store, *, profile: ClientProfile | None = None, rebase: bool = True,
         timeout: float = 60) -> dict:
    """Bring this clone to the canonical HEAD: fetch the chain's snapshot if objects are
    missing, fast-forward, and (with `rebase`) replay unaccepted local commits on top."""
    t0 = time.perf_counter()
    cfg, git = repo.config, repo.git
    onchain = chain.get_repository(cfg["repo_id"])
    if not onchain:
        raise SyncError("repository is not registered on the chain")
    head_q = onchain["canonical_head"]
    _, head = unqualify_oid(head_q)
    before = git.head()
    steps, warnings = [], []
    imported = None

    if not git.has_commit(head):
        snap = onchain.get("snapshot")
        if not snap:
            raise SyncError(f"the canonical HEAD {head_q[:17]}… is not in your repository and nobody has published "
                            "a snapshot of it on the chain yet (ask its author to publish)")
        if store is None:
            raise SyncError("P2P is off: cannot fetch the canonical HEAD's Git objects")
        from .p2p import import_share

        imported = import_share(repo.path.parent, chain, store, snap["cid"], timeout=timeout)
        steps.append(f"fetched snapshot {snap['cid'][:20]}… (verified against the chain)")
        # import_share's "rebase them" advice is what this function does next.
        warnings += [w for w in imported.get("warnings", []) if not w.startswith("your local commits do not build")]
        if snap["head"] != head_q:
            warnings.append(f"the newest published snapshot is for {snap['head'][:17]}…, older than the canonical "
                            f"HEAD {head_q[:17]}…; ask the author of the newest PR to publish")
        if not git.has_commit(head):
            out = verify(repo, chain)
            return {"status": "partial", "steps": steps, "warnings": warnings, "verify": out,
                    "previous_head": before, "local_head": git.head(), "canonical_head": head_q,
                    "took_ms": round((time.perf_counter() - t0) * 1000, 1)}

    rel = relation(git, head)
    rebased = None
    if rel["state"] == "behind":
        try:
            git.run("merge", "-q", "--ff-only", head)
            steps.append(f"fast-forwarded {before[:10]} → {head[:10]}")
        except GitError as e:
            raise SyncError(f"could not fast-forward (uncommitted changes in the way?): {e.stderr}")
    elif rel["state"] == "diverged":
        if not rebase:
            raise SyncError(f"local main diverges from the canonical HEAD {head[:10]}; sync with rebase")
        rebased = _rebase(git, head, profile)
        steps.append(f"rebased {rebased['commits']} local commit(s) onto the canonical HEAD {head[:10]}")
    elif rel["state"] == "in-sync":
        steps.append("already at the canonical HEAD")
    elif rel["state"] == "ahead":
        steps.append(f"already on top of the canonical HEAD ({rel['ahead']} local commit(s) to submit)")

    out = verify(repo, chain)
    return {"status": out["status"], "steps": steps, "warnings": warnings, "rebased": rebased,
            "imported": imported, "verify": out, "previous_head": before, "local_head": git.head(),
            "canonical_head": head_q, "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


def _rebase(git, onto: str, profile: ClientProfile | None) -> dict:
    """Replay local commits that are not canonical onto `onto` (aborts cleanly on conflicts)."""
    base = git.run("merge-base", "HEAD", onto).strip()
    count = int(git.run("rev-list", "--count", f"{base}..HEAD").strip())
    old = git.head()
    ident = []
    if profile:
        ident = ["-c", f"user.name={profile.name}", "-c", f"user.email={profile.email}"]
    try:
        git.run(*ident, "rebase", "--autostash", "-q", "--onto", onto, base)
    except GitError as e:
        conflicts = git.run("diff", "--name-only", "--diff-filter=U", check=False).split()
        git.run("rebase", "--abort", check=False)
        raise SyncError("your local commits conflict with the canonical HEAD"
                        + (f" in {', '.join(conflicts)}" if conflicts else "")
                        + f"; resolve by hand: git rebase {onto[:10]}  ({e.stderr.strip()[:200]})")
    return {"commits": count, "old_head": old, "new_head": git.head(), "onto": onto}


def clone_from_chain(workspace, chain, store, repo_id: str, timeout: float = 60) -> dict:
    """Clone a repository listed on the chain, from the snapshot the chain points at."""
    onchain = chain.get_repository(repo_id, events=False) if chain.kind == "evm" else chain.get_repository(repo_id)
    if not onchain:
        raise SyncError("that repository is not registered on the chain")
    snap = onchain.get("snapshot")
    if not snap:
        raise SyncError(f"{onchain['name']} has no published snapshot on the chain yet; its author must publish "
                        "(Share tab → Publish)")
    from .p2p import import_share

    res = import_share(Path(workspace), chain, store, snap["cid"], timeout=timeout)
    res["snapshot"] = snap
    return res


def land(repo: DosrRepo, chain, request_id: str) -> dict:
    """Submit a saved, approved attestation to the chain (a PR reviewed with register off)."""
    import json

    d = repo.submissions_dir / request_id
    resp_file = d / "response.json"
    if not resp_file.exists():
        raise SyncError(f"submission {request_id} has no attestor response")
    if (d / "chain-event.json").exists():
        raise SyncError("this submission is already on the chain")
    response = json.loads(resp_file.read_text("utf-8"))
    if not response.get("attestation", {}).get("approved"):
        raise SyncError("only approved attestations can land on the chain")
    event = chain.accept_transition(response)
    record_landed(repo, request_id, event)
    return event


def record_landed(repo: DosrRepo, request_id: str, event: dict) -> None:
    """Save the chain event of a submission that landed after its review."""
    import json

    d = repo.submissions_dir / request_id
    (d / "chain-event.json").write_text(json.dumps(event, indent=2), encoding="utf-8")
    run_file = d / "run.json"
    if run_file.exists():
        run = json.loads(run_file.read_text("utf-8"))
        run.setdefault("info", {})["chain_event"] = event
        run["outcome"] = "approved"
        run["info"].pop("chain_conflict", None)
        run_file.write_text(json.dumps(run, indent=2), encoding="utf-8")


def record_conflict(repo: DosrRepo, request_id: str, error) -> None:
    """Mark a saved submission as having lost the race (for History)."""
    import json

    run_file = repo.submissions_dir / request_id / "run.json"
    if run_file.exists():
        run = json.loads(run_file.read_text("utf-8"))
        run["outcome"] = "conflict"
        run.setdefault("info", {})["chain_conflict"] = {"error": str(error), "head": getattr(error, "head", None),
                                                        "tx_hash": getattr(error, "tx_hash", None)}
        run_file.write_text(json.dumps(run, indent=2), encoding="utf-8")


__all__ = ["SyncError", "verify", "sync", "clone_from_chain", "land", "record_landed", "record_conflict", "relation"]
