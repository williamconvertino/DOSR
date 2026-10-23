"""Labeled review cases, and turning each one into a real Git parent -> candidate pair.

A case is a directory holding `case.json` (schema in experiments/README.md). Its
`change` says where the code comes from:

- "files": a base snapshot plus files laid over it (both directories inside the case).
- "demo":  a project/change pair from demo/projects (no copying of demo files).
- "git":   two commits of a public repository, fetched by SHA. `reverse: true`
           proposes going back from `head` to `base` (e.g. undoing a security fix).

Commits are made with a fixed identity and date, so the same case always yields the
same OIDs and therefore the same prompt (and `review_input_hash`).
"""

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dosr_git import GitRepo

ROOT = Path(__file__).resolve().parents[2]
DEMO_PROJECTS = ROOT / "demo" / "projects"

CATEGORIES = ("benign", "bug", "vuln", "injection", "deceptive")
EXPECTED = ("approve", "reject")
# Optional 2x2 labels (paper matrix): how well the PR is presented x whether the code
# is right. Policy: only a high-quality PR with good code is accepted.
QUALITIES = ("high", "low")
CODES = ("good", "bad")

_IDENT = {
    "GIT_AUTHOR_NAME": "DOSR Eval",
    "GIT_AUTHOR_EMAIL": "eval@dosr.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "DOSR Eval",
    "GIT_COMMITTER_EMAIL": "eval@dosr.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


class CaseError(ValueError):
    pass


@dataclass
class Case:
    id: str
    category: str
    expected: str
    change: dict
    dir: Path
    repo_name: str
    message: str | None = None
    target_issue: str | None = None  # withheld from the model; for diagnosis grading
    source: dict = field(default_factory=dict)
    notes: str = ""
    quality: str | None = None  # "high" | "low": description, tests, clarity
    code: str | None = None  # "good" | "bad": correct and safe, or not

    @property
    def cell(self) -> str | None:
        """Matrix cell, e.g. "high-good"; None unless both labels are set."""
        return f"{self.quality}-{self.code}" if self.quality and self.code else None


@dataclass
class Materialized:
    case: Case
    git: GitRepo
    parent: str
    candidate: str
    object_format: str


def load_case(case_dir: Path) -> Case:
    path = Path(case_dir) / "case.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise CaseError(f"{path}: {e}")
    missing = {"id", "category", "expected", "change"} - set(data)
    if missing:
        raise CaseError(f"{path}: missing {', '.join(sorted(missing))}")
    if data["category"] not in CATEGORIES:
        raise CaseError(f"{path}: category must be one of {', '.join(CATEGORIES)}")
    if data["expected"] not in EXPECTED:
        raise CaseError(f"{path}: expected must be approve or reject")
    if data["change"].get("type") not in ("files", "demo", "git"):
        raise CaseError(f"{path}: change.type must be files, demo, or git")
    quality, code = data.get("quality"), data.get("code")
    if quality is not None and quality not in QUALITIES:
        raise CaseError(f"{path}: quality must be high or low")
    if code is not None and code not in CODES:
        raise CaseError(f"{path}: code must be good or bad")
    if quality and code:
        implied = "approve" if (quality, code) == ("high", "good") else "reject"
        if data["expected"] != implied:
            raise CaseError(f"{path}: a {quality}-quality PR with {code} code must expect {implied}")
    return Case(
        id=data["id"],
        category=data["category"],
        expected=data["expected"],
        change=data["change"],
        dir=Path(case_dir).resolve(),
        repo_name=data.get("repo_name") or data["id"],
        message=data.get("message"),
        target_issue=data.get("target_issue"),
        source=data.get("source", {}),
        notes=data.get("notes", ""),
        quality=quality,
        code=code,
    )


def load_cases(cases_dir: Path, ids: list[str] | None = None) -> list[Case]:
    cases = [load_case(p.parent) for p in sorted(Path(cases_dir).glob("*/case.json"))]
    seen = set()
    for c in cases:
        if c.id in seen:
            raise CaseError(f"duplicate case id {c.id!r}")
        seen.add(c.id)
    if ids:
        unknown = set(ids) - seen
        if unknown:
            raise CaseError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in ids]
    return cases


# ---------------------------------------------------------------- materializing

def _git(cwd: Path, *args: str, input: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_IDENT, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=cwd, env=env,
                          input=input.encode() if input is not None else None, capture_output=True)
    if proc.returncode != 0:
        raise CaseError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace").strip()


def _overlay(src: Path, dst: Path) -> None:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)


def _snapshot_pair(case: Case, work: Path, base: Path, head: Path | None, delete: list[str],
                   message: str) -> Materialized:
    repo = work / case.id
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _overlay(base, repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Initial commit")
    parent = _git(repo, "rev-parse", "HEAD")
    if head is not None:
        _overlay(head, repo)
    for rel in delete:
        (repo / rel).unlink(missing_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-F", "-", input=message)
    candidate = _git(repo, "rev-parse", "HEAD")
    if candidate == parent:
        raise CaseError(f"{case.id}: change produced no commit")
    return Materialized(case, GitRepo(repo), parent, candidate, "sha1")


def _demo(case: Case, work: Path) -> Materialized:
    project, change = case.change["project"], case.change["change"]
    pdir = DEMO_PROJECTS / project
    message = case.message
    if message is None:
        seed = json.loads((pdir / "seed.json").read_text("utf-8"))
        message = next((c["message"] for c in seed.get("demo_changes", []) if c["id"] == change), None)
        if message is None:
            raise CaseError(f"{case.id}: no message given and none in {project}/seed.json for {change}")
    return _snapshot_pair(case, work, pdir / "files", pdir / "changes" / change,
                          case.change.get("delete", []), message)


def _files(case: Case, work: Path) -> Materialized:
    if case.message is None:
        raise CaseError(f"{case.id}: files cases need a `message`")
    head = case.change.get("head")
    return _snapshot_pair(case, work, case.dir / case.change["base"], case.dir / head if head else None,
                          case.change.get("delete", []), case.message)


def _git_source(case: Case, work: Path, cache: Path) -> Materialized:
    url, base, head = case.change["url"], case.change["base"], case.change["head"]
    repo = cache / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".git")
    if not repo.is_dir():
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "--bare")
        _git(repo, "remote", "add", "origin", url)
        # Partial clone: blobs are fetched lazily, only for the files the PR shows.
        _git(repo, "config", "remote.origin.promisor", "true")
        _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    missing = [sha for sha in (base, head)
               if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo,
                                 capture_output=True).returncode != 0]
    if missing:
        _git(repo, "fetch", "-q", "--filter=blob:none", "origin", *missing)
    base, head = _git(repo, "rev-parse", f"{base}^{{commit}}"), _git(repo, "rev-parse", f"{head}^{{commit}}")

    if case.change.get("reverse"):
        # Propose undoing base..head: parent is head, candidate has base's tree.
        parent, tree = head, f"{base}^{{tree}}"
    else:
        parent, tree = base, None
        if _git(repo, "merge-base", base, head) != base:
            raise CaseError(f"{case.id}: {head[:10]} does not descend from {base[:10]}")

    if tree is None and case.message is None:
        candidate = head  # the real commits, real messages
    else:
        tree = tree or f"{head}^{{tree}}"
        message = case.message or f"Revert {head[:12]}"
        # Squash into one synthetic commit carrying the case's message.
        candidate = _git(repo, "commit-tree", tree, "-p", parent, "-F", "-", input=message)
    return Materialized(case, GitRepo(repo), parent, candidate, _git(repo, "rev-parse", "--show-object-format"))


def materialize(case: Case, work: Path, cache: Path) -> Materialized:
    kind = case.change["type"]
    if kind == "demo":
        return _demo(case, Path(work))
    if kind == "files":
        return _files(case, Path(work))
    return _git_source(case, Path(work), Path(cache))
