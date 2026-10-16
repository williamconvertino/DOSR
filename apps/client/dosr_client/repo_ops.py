"""Repository lifecycle: create (genesis), inspect status, commit."""

import json
import shutil
import time
from pathlib import Path

from dosr_chain import ChainError, MockChain
from dosr_git import GitRepo
from dosr_protocol.canonical import derive_repo_id, policy_hash, qualify_oid, unqualify_oid
from dosr_protocol.policy import validate_policy

from .config import POLICY_FILE, REPO_FILE, ClientProfile, ConfigError, DosrRepo

_SEED_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv")


def create_repository(
    path,
    *,
    name: str,
    policy: dict,
    profile: ClientProfile,
    chain: MockChain,
    description: str = "",
    seed_dir=None,
    register: bool = True,
) -> dict:
    """Genesis: git init + seed files + .dosr/ config, first commit, register on chain."""
    path = Path(path).resolve()
    if (path / ".git").exists():
        raise ConfigError(f"{path} is already a git repository; DOSR repos must start from genesis")
    if path.exists() and any(path.iterdir()) and seed_dir:
        raise ConfigError(f"{path} is not empty; cannot copy a seed project into it")
    validate_policy(policy)

    if seed_dir:
        shutil.copytree(seed_dir, path, ignore=_SEED_IGNORE, dirs_exist_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    git = GitRepo.init(path)

    p_hash = policy_hash(policy)
    repo_cfg = {
        "dosr_repo_version": "0.1",
        "name": name,
        "description": description,
        "repo_id": derive_repo_id(name, p_hash, profile.email),
        "chain_id": chain.chain_id,
        "contract": chain.contract,
        "policy_hash": p_hash,
        "created_by": f"{profile.name} <{profile.email}>",
        "created_at": int(time.time()),
    }
    (path / ".dosr").mkdir(exist_ok=True)
    (path / POLICY_FILE).write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    (path / REPO_FILE).write_text(json.dumps(repo_cfg, indent=2) + "\n", encoding="utf-8")
    if not (path / "README.md").exists():
        (path / "README.md").write_text(f"# {name}\n\n{description}\n", encoding="utf-8")

    git.add_all()
    genesis = git.commit(f"DOSR genesis: {name}\n\n{description}".strip(), profile.name, profile.email)
    genesis_q = qualify_oid(git.object_format(), genesis)

    registration = None
    if register:
        registration = chain.register_repository(
            repo_id=repo_cfg["repo_id"],
            name=name,
            policy=policy,
            genesis_git_oid=genesis_q,
            object_format=git.object_format(),
        )
    return {"path": str(path), "repo": repo_cfg, "genesis_git_oid": genesis_q, "registration": registration}


def register_existing(repo: DosrRepo, chain: MockChain) -> dict:
    """Register an already-initialized repo whose genesis was never put on the chain
    (e.g. the chain state was reset). Uses the root commit as genesis."""
    root = repo.git.run("rev-list", "--max-parents=0", "HEAD").split()[0]
    return chain.register_repository(
        repo_id=repo.config["repo_id"],
        name=repo.name,
        policy=repo.policy,
        genesis_git_oid=qualify_oid(repo.git.object_format(), root),
        object_format=repo.git.object_format(),
    )


def repo_status(repo: DosrRepo, chain: MockChain) -> dict:
    cfg = repo.config
    git = repo.git
    head = git.head()
    fmt = git.object_format()

    status = {
        "name": repo.name,
        "path": str(repo.path),
        "description": cfg.get("description", ""),
        "repo_id": cfg["repo_id"],
        "chain_id": cfg["chain_id"],
        "contract": cfg["contract"],
        "policy_hash": cfg["policy_hash"],
        "policy_hash_ok": None,
        "branch": git.current_branch(),
        "local_head": qualify_oid(fmt, head) if head else None,
        "canonical_head": None,
        "registered": False,
        "chain_error": None,
        "ahead": 0,
        "in_sync": False,
        "diverged": False,
        "changes": git.status(),
        "policy": None,
    }
    try:
        status["policy"] = repo.policy
        status["policy_hash_ok"] = repo.computed_policy_hash() == cfg["policy_hash"]
    except Exception as e:  # malformed policy file should not break status
        status["policy_error"] = str(e)

    try:
        onchain = chain.get_repository(cfg["repo_id"])
    except Exception as e:
        onchain, status["chain_error"] = None, str(e)
    if onchain:
        status["registered"] = True
        status["canonical_head"] = onchain["canonical_head"]
        status["accepted_transitions"] = onchain["accepted_transition_count"]
        status["policy_hash_ok"] = status["policy_hash_ok"] and onchain["policy_hash"] == cfg["policy_hash"]
        _, canon_hex = unqualify_oid(onchain["canonical_head"])
        if head and git.has_commit(canon_hex):
            status["in_sync"] = canon_hex == head
            if git.is_ancestor(canon_hex, head):
                status["ahead"] = int(git.run("rev-list", "--count", f"{canon_hex}..{head}").strip())
            else:
                status["diverged"] = True
        else:
            status["diverged"] = True
    elif not status["chain_error"]:
        status["chain_error"] = "repository is not registered on the (mock) chain"
    return status


def commit_all(repo: DosrRepo, message: str, profile: ClientProfile) -> str:
    if not message.strip():
        raise ConfigError("a commit message is required")
    if not repo.git.is_dirty():
        raise ConfigError("nothing to commit (working tree clean)")
    repo.git.add_all()
    return repo.git.commit(message, profile.name, profile.email)


__all__ = ["create_repository", "register_existing", "repo_status", "commit_all", "ChainError"]
