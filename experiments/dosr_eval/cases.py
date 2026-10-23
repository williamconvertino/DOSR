"""Labeled review cases, and turning each one into a real Git parent -> candidate pair.

A case is a directory holding `case.json` (schema in experiments/README.md). Its
`change` says where the code comes from:

- "files": a base snapshot plus files laid over it (both directories inside the case).
- "demo":  a project/change pair from demo/projects (no copying of demo files).
- "git":   two commits of a public repository, fetched by SHA. `reverse: true`
           proposes going back from `head` to `base` (e.g. undoing a security fix).
- "patch": a commit of a public repository (`base`, fetched by SHA) plus a diff file
           inside the case directory, applied on top of it (e.g. a SWE-bench patch).

Cases may sit in nested directories (`cases/pilot-v1/<case>/case.json`); pointing
--cases at a subdirectory selects that suite.

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
CHANGE_TYPES = ("files", "demo", "git", "patch")

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
    if data["change"].get("type") not in CHANGE_TYPES:
        raise CaseError(f"{path}: change.type must be one of {', '.join(CHANGE_TYPES)}")
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
    )


def load_cases(cases_dir: Path, ids: list[str] | None = None) -> list[Case]:
    cases = [load_case(p.parent) for p in sorted(Path(cases_dir).glob("**/case.json"))]
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

def _git(cwd: Path, *args: str, input: str | None = None, index: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_IDENT, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
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


def _fetched(cache: Path, url: str, shas: list[str], shallow: bool = False) -> Path:
    """A cached bare partial clone of `url` holding the given commits (with their
    history, or only the commits themselves when `shallow`)."""
    key = url + ("#shallow" if shallow else "")
    repo = cache / (hashlib.sha256(key.encode()).hexdigest()[:16] + ".git")
    if not repo.is_dir():
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "--bare")
        _git(repo, "remote", "add", "origin", url)
        # Partial clone: blobs are fetched lazily, only for the files the PR shows.
        _git(repo, "config", "remote.origin.promisor", "true")
        _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    missing = [sha for sha in shas
               if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo,
                                 capture_output=True).returncode != 0]
    if missing:
        _git(repo, "fetch", "-q", "--filter=blob:none", *(["--depth=1"] if shallow else []), "origin", *missing)
    return repo


def _git_source(case: Case, work: Path, cache: Path) -> Materialized:
    url, base, head = case.change["url"], case.change["base"], case.change["head"]
    repo = _fetched(cache, url, [base, head])
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


def _patch_source(case: Case, work: Path, cache: Path) -> Materialized:
    if case.message is None:
        raise CaseError(f"{case.id}: patch cases need a `message`")
    url = case.change["url"]
    repo = _fetched(cache, url, [case.change["base"]], shallow=True)
    base = _git(repo, "rev-parse", f"{case.change['base']}^{{commit}}")
    patch = (case.dir / case.change["patch"]).read_text("utf-8")
    if not patch.endswith("\n"):
        patch += "\n"
    # Apply to a throwaway index built from base's tree: no checkout needed, and the
    # partial clone fetches only the blobs the patch touches.
    work.mkdir(parents=True, exist_ok=True)
    index = work / f"{case.id}.index"
    index.unlink(missing_ok=True)
    try:
        _git(repo, "read-tree", base, index=index)
        _git(repo, "apply", "--cached", "--whitespace=nowarn", "-", input=patch, index=index)
        tree = _git(repo, "write-tree", index=index)
    except CaseError as e:
        raise CaseError(f"{case.id}: patch does not apply to {base[:10]}: {e}")
    finally:
        index.unlink(missing_ok=True)
    if tree == _git(repo, "rev-parse", f"{base}^{{tree}}"):
        raise CaseError(f"{case.id}: patch changes nothing")
    candidate = _git(repo, "commit-tree", tree, "-p", base, "-F", "-", input=case.message)
    return Materialized(case, GitRepo(repo), base, candidate, _git(repo, "rev-parse", "--show-object-format"))


def materialize(case: Case, work: Path, cache: Path) -> Materialized:
    kind = case.change["type"]
    if kind == "demo":
        return _demo(case, Path(work))
    if kind == "files":
        return _files(case, Path(work))
    if kind == "patch":
        return _patch_source(case, Path(work), Path(cache))
    return _git_source(case, Path(work), Path(cache))
