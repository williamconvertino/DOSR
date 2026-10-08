"""Client configuration: repository files, local state, and client profiles.

Layout of a DOSR repository:

    <repo>/.dosr/policy.json   immutable review policy (hash registered at genesis;
                               sent verbatim to the attestor)
    <repo>/.dosr/repo.json     public repository metadata (repo_id, chain, contract)
    <repo>/.git/dosr/          local, never-committed client state
        submissions/<request_id>/   artifacts of each PR submission
        timings.json                step latency history (for ETA estimates)

A client *profile* is the identity of whoever is running the client (git author,
preferred review model, attestor URL). The demo ships several fake ones in
demo/clients/.
"""

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dosr_git import GitRepo
from dosr_protocol.canonical import policy_hash
from dosr_protocol.policy import validate_policy

from . import __version__

DEFAULT_ATTESTOR_URL = "http://127.0.0.1:8080"
DOSR_DIR = ".dosr"
POLICY_FILE = f"{DOSR_DIR}/policy.json"
REPO_FILE = f"{DOSR_DIR}/repo.json"
PROTECTED_PATHS = (POLICY_FILE, REPO_FILE)


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------- profiles


@dataclass
class ClientProfile:
    id: str
    name: str
    email: str
    description: str = ""
    client_version: str = __version__
    attestor_url: str | None = None
    # Optional {provider, model, version}; defaults to the policy's first approved model.
    review_model: dict | None = None
    bundle_mode: str = "full"
    color: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def load_profile(path) -> ClientProfile:
    path = Path(path)
    data = json.loads(path.read_text("utf-8"))
    known = {k: data.pop(k) for k in list(data) if k in ClientProfile.__dataclass_fields__}
    known.setdefault("id", path.stem)
    return ClientProfile(**known, extra=data)


def default_profile() -> ClientProfile:
    def git_config(key, fallback):
        try:
            out = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True)
            return out.stdout.strip() or fallback
        except OSError:
            return fallback

    return ClientProfile(
        id="default",
        name=git_config("user.name", "DOSR User"),
        email=git_config("user.email", "user@dosr.local"),
        description="Local git identity",
    )


def resolve_profile(path: str | None = None) -> ClientProfile:
    path = path or os.environ.get("DOSR_PROFILE")
    return load_profile(path) if path else default_profile()


def profiles_in_dir(directory) -> list[ClientProfile]:
    if not directory or not Path(directory).is_dir():
        return []
    return [load_profile(p) for p in sorted(Path(directory).glob("*.json"))]


def resolve_attestor_url(explicit: str | None, profile: ClientProfile | None) -> str:
    return (
        explicit
        or os.environ.get("DOSR_ATTESTOR_URL")
        or (profile.attestor_url if profile else None)
        or DEFAULT_ATTESTOR_URL
    ).rstrip("/")


# ---------------------------------------------------------------- repository


class DosrRepo:
    """A git working copy that has been initialized as a DOSR repository."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not GitRepo.is_repo(self.path):
            raise ConfigError(f"{self.path} is not a git repository")
        if not (self.path / REPO_FILE).exists():
            raise ConfigError(f"{self.path} is not a DOSR repository (missing {REPO_FILE}); run `dosr init`")
        self.git = GitRepo(self.path)

    @classmethod
    def find(cls, start=".") -> "DosrRepo":
        p = Path(start).resolve()
        for candidate in (p, *p.parents):
            if (candidate / REPO_FILE).exists():
                return cls(candidate)
        raise ConfigError(f"no DOSR repository found at or above {p}")

    @staticmethod
    def is_dosr_repo(path) -> bool:
        return (Path(path) / REPO_FILE).exists() and GitRepo.is_repo(path)

    @property
    def name(self) -> str:
        return self.config.get("name") or self.path.name

    @property
    def config(self) -> dict:
        return json.loads((self.path / REPO_FILE).read_text("utf-8"))

    def policy_bytes(self) -> bytes:
        return (self.path / POLICY_FILE).read_bytes()

    @property
    def policy(self) -> dict:
        policy = json.loads(self.policy_bytes())
        validate_policy(policy)
        return policy

    def computed_policy_hash(self) -> str:
        return policy_hash(self.policy)

    @property
    def state_dir(self) -> Path:
        d = self.git.git_dir / "dosr"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def submissions_dir(self) -> Path:
        d = self.state_dir / "submissions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_submissions(self) -> list[dict]:
        runs = []
        for run_file in self.submissions_dir.glob("*/run.json"):
            try:
                runs.append(json.loads(run_file.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(runs, key=lambda r: r.get("started_at", 0), reverse=True)

    def load_submission(self, request_id: str) -> dict:
        d = self.submissions_dir / request_id
        if not d.is_dir() or "/" in request_id or "\\" in request_id or ".." in request_id:
            raise ConfigError(f"no submission {request_id}")
        out = {}
        for name in ("run.json", "request.json", "response.json", "pr.json"):
            f = d / name
            if f.exists():
                out[name.removesuffix(".json")] = json.loads(f.read_text("utf-8"))
        if (d / "pr.md").exists():
            out["pr_markdown"] = (d / "pr.md").read_text("utf-8")
        out["dir"] = str(d)
        return out
