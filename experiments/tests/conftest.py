import json
import os
import subprocess
import sys
from pathlib import Path

EXPERIMENTS = Path(__file__).resolve().parents[1]
CASES = EXPERIMENTS / "cases"
sys.path.insert(0, str(Path(__file__).parent))


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def write_case(root: Path, data: dict, files: dict[str, str] | None = None) -> Path:
    d = root / data["id"]
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps(data), encoding="utf-8")
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d
