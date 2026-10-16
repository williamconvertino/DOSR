"""Thin wrapper over the `git` CLI.

All repository content handling goes through real Git so commit/tree/blob OIDs
stay canonical. Only stdlib + the `git` executable are required.
"""

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args, returncode, stderr):
        self.git_args = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {self.stderr}")


_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


class GitRepo:
    def __init__(self, path):
        self.path = Path(path).resolve()

    # ---------- plumbing ----------

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        return self.run_bytes(*args, check=check, input=input.encode() if input else None).decode(
            "utf-8", errors="replace"
        )

    def run_bytes(self, *args: str, check: bool = True, input: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=self.path,
            input=input,
            capture_output=True,
            env=_ENV,
        )
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    @classmethod
    def init(cls, path, initial_branch: str = "main") -> "GitRepo":
        Path(path).mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        repo.run("init", "-q", "-b", initial_branch)
        return repo

    @classmethod
    def is_repo(cls, path) -> bool:
        return (Path(path) / ".git").exists()

    @property
    def git_dir(self) -> Path:
        return Path(self.run("rev-parse", "--absolute-git-dir").strip())

    # ---------- refs / objects ----------

    def head(self) -> str | None:
        out = self.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).strip()
        return out or None

    def current_branch(self) -> str | None:
        out = self.run("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
        return out or None

    def object_format(self) -> str:
        out = self.run("rev-parse", "--show-object-format", check=False).strip()
        return out or "sha1"

    def resolve(self, rev: str) -> str:
        return self.run("rev-parse", "--verify", f"{rev}^{{commit}}").strip()

    def _succeeds(self, *args: str) -> bool:
        proc = subprocess.run(["git", *args], cwd=self.path, capture_output=True, env=_ENV)
        return proc.returncode == 0

    def has_commit(self, oid: str) -> bool:
        return self._succeeds("cat-file", "-e", f"{oid}^{{commit}}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._succeeds("merge-base", "--is-ancestor", ancestor, descendant)

    def update_ref(self, ref: str, oid: str) -> None:
        self.run("update-ref", ref, oid)

    def delete_ref(self, ref: str) -> None:
        self.run("update-ref", "-d", ref, check=False)

    # ---------- working tree ----------

    def status(self) -> list[dict]:
        """Porcelain status entries: {code, path} (code is the 2-char XY status)."""
        out = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries, parts, i = [], out.split("\0"), 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if not item:
                continue
            code, path = item[:2], item[3:]
            if code[0] in "RC":
                i += 1  # skip the rename source path
            entries.append({"code": code, "path": path})
        return entries

    def is_dirty(self) -> bool:
        return bool(self.status())

    def worktree_files(self) -> list[str]:
        out = self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        paths = {p for p in out.split("\0") if p}
        return sorted(p for p in paths if (self.path / p).is_file())

    def add_all(self) -> None:
        self.run("add", "-A")

    def commit(self, message: str, author_name: str | None = None, author_email: str | None = None,
               allow_empty: bool = False) -> str:
        ident = []
        if author_name:
            ident += ["-c", f"user.name={author_name}"]
        if author_email:
            ident += ["-c", f"user.email={author_email}"]
        args = [*ident, "commit", "-q", "--no-verify", "-F", "-"]
        if allow_empty:
            args.append("--allow-empty")
        self.run(*args, input=message)
        return self.head()

    # ---------- history / diffs ----------

    def commits_between(self, parent: str, candidate: str) -> list[dict]:
        """Commits in parent..candidate, oldest first."""
        sep, end = "\x1f", "\x1e"
        fmt = sep.join(["%H", "%an", "%ae", "%aI", "%s", "%b"]) + end
        out = self.run("log", "--reverse", f"--format={fmt}", f"{parent}..{candidate}")
        commits = []
        for rec in out.split(end):
            rec = rec.strip("\n")
            if not rec:
                continue
            oid, name, email, date, subject, body = (rec.split(sep) + [""] * 6)[:6]
            commits.append(
                {"oid": oid, "author": f"{name} <{email}>", "date": date, "subject": subject,
                 "body": body.strip()}
            )
        return commits

    def changed_files(self, parent: str, candidate: str) -> list[dict]:
        status_out = self.run("diff", "--name-status", "-z", "-M", parent, candidate)
        numstat_out = self.run("diff", "--numstat", "-z", "-M", parent, candidate)

        files, parts, i = [], status_out.split("\0"), 0
        while i < len(parts):
            code = parts[i]
            if not code:
                i += 1
                continue
            if code[0] in "RC":
                old, new = parts[i + 1], parts[i + 2]
                files.append({"path": new, "old_path": old, "status": code[0]})
                i += 3
            else:
                files.append({"path": parts[i + 1], "status": code[0]})
                i += 2

        # numstat -z: "<add>\t<del>\t<path>\0" or for renames "<add>\t<del>\t\0<old>\0<new>\0"
        stats, parts, i = {}, numstat_out.split("\0"), 0
        while i < len(parts):
            rec = parts[i]
            if not rec:
                i += 1
                continue
            add, dele, path = rec.split("\t", 2)
            if path == "":
                path = parts[i + 2]
                i += 3
            else:
                i += 1
            to_int = lambda v: None if v == "-" else int(v)
            stats[path] = (to_int(add), to_int(dele))
        for f in files:
            f["additions"], f["deletions"] = stats.get(f["path"], (None, None))
            f["binary"] = f["additions"] is None and f["status"] != "D"
        return files

    def diff(self, parent: str, candidate: str, context_lines: int = 3) -> str:
        return self.run(
            "diff", "--no-color", "--no-ext-diff", "-M", f"-U{context_lines}", parent, candidate
        )

    def diff_worktree(self, base: str, context_lines: int = 3) -> str:
        """Diff from `base` to the working tree, including untracked files."""
        out = self.run("diff", "--no-color", "--no-ext-diff", "-M", f"-U{context_lines}", base)
        for entry in self.status():
            if entry["code"] == "??":
                out += self.run(
                    "diff", "--no-color", "--no-index", f"-U{context_lines}", "/dev/null",
                    entry["path"], check=False,
                )
        return out

    def show_file(self, rev: str, path: str, max_bytes: int | None = None) -> str | None:
        """Text content of `path` at `rev`; None if missing, binary, or too large."""
        try:
            data = self.run_bytes("show", f"{rev}:{path}")
        except GitError:
            return None
        if b"\0" in data[:8000] or (max_bytes is not None and len(data) > max_bytes):
            return None
        return data.decode("utf-8", errors="replace")

    def ls_tree(self, rev: str) -> list[str]:
        out = self.run("ls-tree", "-r", "-z", "--name-only", rev)
        return [p for p in out.split("\0") if p]
