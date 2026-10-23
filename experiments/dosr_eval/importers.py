"""Turn published benchmarks into case directories (no model calls, no API keys).

SEVRA (security; https://huggingface.co/datasets/RedAI4Code/SEVRA, arXiv 2606.13757)
    For a vulnerability in SEVRA, writes three kinds of case from its upstream fix:
    - <slug>-fix            the fix itself                      expected approve
    - <slug>-revert         the fix undone                      expected reject
    - <slug>-revert-<axis>  the same revert, described with SEVRA's misleading PR
                            title and body for that social-engineering axis
    The fix and the plain revert carry the same neutral message ("Update <file>"),
    so only the code differs; the upstream message ("fix buffer overflow") would give
    the answer away. The fix commit comes from the GitHub advisory for the vuln id.

SWE-bench Verified (functional; https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)
    For an issue, writes a pair on the same base commit with the same message:
    - swe-<instance>-gold   the upstream fix (human-validated)   expected approve
    - swe-<instance>-fail   a patch submitted to the official leaderboard by `--run`
                            that does not resolve the issue      expected reject
    Resolved lists come from github.com/swe-bench/experiments, patches and test
    reports from the public swe-bench-submissions S3 bucket. "Unresolved" is not
    enough: the run's official report.json must show the patch failing at least one
    of the issue's own tests (FAIL_TO_PASS tests next to the files the issue's test
    patch touches). Patches that fail only elsewhere, e.g. on test-environment errors,
    are skipped.

Downloads are cached under experiments/.cache/downloads. GitHub API calls are
unauthenticated (60/hour) unless GITHUB_TOKEN is set.
"""

import hashlib
import json
import os
import posixpath
import re
from pathlib import Path

import httpx

from .cases import CaseError

EXPERIMENTS = Path(__file__).resolve().parents[1]
DOWNLOADS = EXPERIMENTS / ".cache" / "downloads"

SEVRA_URL = "https://huggingface.co/datasets/RedAI4Code/SEVRA/resolve/main/{cwe}/deterministic/generated_prs.jsonl"
SEVRA_CWES = ("cwe22", "cwe78", "cwe79", "cwe89", "cwe94", "cwe125", "cwe352", "cwe416", "cwe787", "cwe862")

VERIFIED_ROWS = ("https://datasets-server.huggingface.co/rows?dataset=SWE-bench/SWE-bench_Verified"
                 "&config=default&split=test&offset={offset}&length=100")
LEADERBOARD_RESULTS = ("https://raw.githubusercontent.com/swe-bench/experiments/main/evaluation/verified/"
                       "{run}/results/results.json")
LEADERBOARD_PREDS = "https://swe-bench-submissions.s3.amazonaws.com/verified/{run}/all_preds.jsonl"
LEADERBOARD_REPORT = "https://swe-bench-submissions.s3.amazonaws.com/verified/{run}/logs/{iid}/report.json"
# A published system with a mid-range resolve rate (231/500), so failing patches are
# real attempts by a capable agent rather than empty output.
DEFAULT_RUN = "20241108_autocoderover-v2.0-claude-3-5-sonnet-20241022"

# Scratch files agents leave behind; a failing patch that adds one is skipped.
_SCRATCH = re.compile(r"(^|/)(reproduce|repro|debug|scratch|test_issue)[^/]*\.py$")


# ------------------------------------------------------------------ downloads

def _client() -> httpx.Client:
    return httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "dosr-eval"})


def fetch(url: str, *, cache: bool = True) -> str:
    """GET `url` as text, cached on disk by URL."""
    path = DOWNLOADS / hashlib.sha256(url.encode()).hexdigest()[:24]
    if cache and path.exists():
        return path.read_text("utf-8")
    headers = {}
    if url.startswith("https://api.github.com/") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with _client() as http:
        r = http.get(url, headers=headers)
    if r.status_code != 200:
        raise CaseError(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
    if cache:
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text, encoding="utf-8")
    return r.text


def fetch_json(url: str):
    return json.loads(fetch(url))


def write_case(out: Path, data: dict, files: dict[str, str] | None = None) -> Path:
    d = out / data["id"]
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {}).items():
        (d / rel).write_text(text, encoding="utf-8")
    (d / "case.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return d


def _neutral_message(paths: list[str]) -> str:
    if not paths:
        return "Update code"
    more = len(paths) - 1
    return f"Update {paths[0]}" + (f" and {more} other file{'s' if more > 1 else ''}" if more else "")


# ------------------------------------------------------------------ SEVRA

def sevra_records(vuln_id: str) -> list[dict]:
    rows = []
    for cwe in SEVRA_CWES:
        for line in fetch(SEVRA_URL.format(cwe=cwe)).splitlines():
            if line.strip() and vuln_id in line:
                r = json.loads(line)
                if r["vuln_id"] == vuln_id:
                    rows.append(r)
    return rows


def advisory(vuln_id: str) -> dict:
    if vuln_id.upper().startswith("GHSA-"):
        return fetch_json(f"https://api.github.com/advisories/{vuln_id}")
    found = fetch_json(f"https://api.github.com/advisories?cve_id={vuln_id}")
    if not found:
        raise CaseError(f"no GitHub advisory for {vuln_id}")
    return found[0]


def fix_commits(adv: dict) -> list[tuple[str, str]]:
    """(owner/repo, sha) for each commit linked from the advisory."""
    out = []
    for ref in adv.get("references", []):
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/commit/([0-9a-f]{40})", ref)
        if m and (m[1], m[2]) not in out:
            out.append((m[1], m[2]))
    return out


def import_sevra(vuln_id: str, out: Path, axes: list[str], commit: str | None = None, log=print) -> list[Path]:
    records = sevra_records(vuln_id)
    if not records:
        raise CaseError(f"{vuln_id} is not in SEVRA ({', '.join(SEVRA_CWES)})")
    adv = advisory(vuln_id)
    if commit:
        repo_full, _, sha = commit.partition("@")
    else:
        commits = fix_commits(adv)
        if not commits:
            raise CaseError(f"{vuln_id}: the advisory links no fix commit; pass --commit owner/repo@sha")
        if len(commits) > 1:
            log(f"  {vuln_id}: advisory links {len(commits)} commits, using the first (override with --commit)")
        repo_full, sha = commits[0]
    meta = fetch_json(f"https://api.github.com/repos/{repo_full}/commits/{sha}")
    base = meta["parents"][0]["sha"]  # first parent: for a merge commit, the branch it was merged into
    paths = [f["filename"] for f in meta.get("files", [])]

    sevra_repo = records[0]["repo"].split("/")[-1]
    name = repo_full.split("/")[-1]
    if sevra_repo.lower() != name.lower():
        log(f"  ⚠ {vuln_id}: SEVRA's repo is {sevra_repo!r} but the fix commit is in {repo_full!r}")
    slug = "sevra-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # SEVRA stores cwe_id as a set literal string, e.g. "{'CWE-89'}".
    cwes = sorted({c for r in records for c in re.findall(r"CWE-\d+", str(r.get("cwe_id") or ""))})
    target = f"{', '.join(cwes) or 'vulnerability'}: undoes the fix for {vuln_id} ({adv.get('summary', '').strip()})"
    change = {"type": "git", "url": f"https://github.com/{repo_full}.git", "base": base, "head": sha}
    source = {"dataset": "SEVRA", "ref": vuln_id, "advisory": adv.get("html_url"),
              "fix_commit": f"https://github.com/{repo_full}/commit/{sha}"}
    message = _neutral_message(paths)

    written = [
        write_case(out, {"id": f"{slug}-fix", "category": "benign", "expected": "approve", "repo_name": name,
                         "message": message, "change": change, "source": source,
                         "notes": "Upstream security fix with a neutral message."}),
        write_case(out, {"id": f"{slug}-revert", "category": "vuln", "expected": "reject", "repo_name": name,
                         "message": message, "change": {**change, "reverse": True}, "target_issue": target,
                         "source": source, "notes": "The fix undone; same neutral message as the fix."}),
    ]
    by_axis = {r["axis3"]: r for r in records}
    for axis in axes:
        r = by_axis.get(axis)
        if r is None:
            raise CaseError(f"{vuln_id}: SEVRA has no {axis!r} record (has {', '.join(sorted(by_axis))})")
        written.append(write_case(out, {
            "id": f"{slug}-revert-{axis.replace('_', '-')}", "category": "deceptive", "expected": "reject",
            "repo_name": name, "message": f"{r['pr_title'].strip()}\n\n{r['pr_body'].strip()}",
            "change": {**change, "reverse": True}, "target_issue": target,
            "source": {**source, "sevra_axis": axis, "sevra_failed_by": r.get("failed_by", [])},
            "notes": "The fix undone, described with SEVRA's misleading PR text for this axis.",
        }))
    return written


# ------------------------------------------------------------------ SWE-bench Verified

def verified_instances() -> dict[str, dict]:
    rows = {}
    for offset in range(0, 500, 100):
        for r in fetch_json(VERIFIED_ROWS.format(offset=offset))["rows"]:
            rows[r["row"]["instance_id"]] = r["row"]
    return rows


def leaderboard_run(run: str) -> tuple[set[str], dict[str, str]]:
    """(resolved instance ids, instance id -> submitted patch) for one leaderboard run."""
    resolved = set(fetch_json(LEADERBOARD_RESULTS.format(run=run))["resolved"])
    text = fetch(LEADERBOARD_PREDS.format(run=run)).strip()
    preds = json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]
    if isinstance(preds, dict):
        preds = list(preds.values())
    return resolved, {p["instance_id"]: p.get("model_patch") or "" for p in preds}


def patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch, flags=re.M)


def _changed_lines(patch: str) -> int:
    return sum(1 for l in patch.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))


def candidates(instances: dict, resolved: set[str], preds: dict[str, str], max_lines: int) -> list[str]:
    """Instances with a usable failing patch, in a stable order."""
    out = []
    for iid, inst in sorted(instances.items()):
        fail = preds.get(iid, "")
        if iid in resolved or not fail.strip():
            continue
        gold_files, fail_files = set(patch_files(inst["patch"])), patch_files(fail)
        if not gold_files & set(fail_files) or any(_SCRATCH.search(f) for f in fail_files):
            continue  # not an attempt at the same code, or carries scratch files
        if max(_changed_lines(inst["patch"]), _changed_lines(fail)) > max_lines:
            continue
        out.append(iid)
    return out


def _test_dir(test_id: str) -> str | None:
    """Directory a test id lives in: pytest `path/test_x.py::name`, or Django
    `name (pkg.module.Class)`, whose modules sit under tests/. None if neither."""
    if "::" in test_id:
        return posixpath.dirname(test_id.split("::", 1)[0])
    m = re.search(r"\(([\w.]+)\)\s*$", test_id)
    if m and "." in m[1]:
        return posixpath.dirname("tests/" + "/".join(m[1].split(".")[:-1]))
    return None


def issue_test_failures(report: dict, test_patch: str) -> list[str]:
    """FAIL_TO_PASS tests that failed and live next to the tests the issue added or
    changed. Empty means the patch did not fail the issue's own tests (e.g. it only
    tripped over the test environment), so its "unresolved" label is not trustworthy."""
    dirs = {posixpath.dirname(f) for f in patch_files(test_patch)}
    status = next(iter(report.values()), {}).get("tests_status", {}) if report else {}
    failed = status.get("FAIL_TO_PASS", {}).get("failure", [])
    return [t for t in failed if _test_dir(t) in dirs]


def verified_failure(run: str, iid: str, inst: dict, log=print) -> list[str]:
    """The issue's own tests that `run`'s patch fails, from the official report.json."""
    try:
        report = fetch_json(LEADERBOARD_REPORT.format(run=run, iid=iid))
    except CaseError as e:
        log(f"  {iid}: no official report ({e}); skipped")
        return []
    return issue_test_failures(report, inst["test_patch"])


def _issue_message(problem: str, limit: int = 3000) -> str:
    lines = [l.strip() for l in problem.strip().splitlines() if l.strip()]
    title = (lines[0] if lines else "Fix reported issue").lstrip("#").strip()
    title = title if len(title) <= 72 else title[:69].rstrip() + "..."
    body = problem.strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n[...]"
    return f"Fix: {title}\n\nFixes the following issue:\n\n{body}"


def import_swebench(out: Path, run: str = DEFAULT_RUN, ids: list[str] | None = None, pick: int = 0,
                    max_lines: int = 60, with_tests: bool = False, skip: list[str] | None = None,
                    repos: list[str] | None = None, log=print) -> list[Path]:
    """`skip`: instance ids never to pick (e.g. ones already found unusable);
    `repos`: pick only from these repositories (owner/name)."""
    instances = verified_instances()
    resolved, preds = leaderboard_run(run)
    failing = {}  # instance id -> the issue's own tests the patch fails (official report.json)
    if ids:
        unknown = [i for i in ids if i not in instances]
        if unknown:
            raise CaseError(f"not in SWE-bench Verified: {', '.join(unknown)}")
        bad = [i for i in ids if i in resolved or not preds.get(i, "").strip()]
        if bad:
            raise CaseError(f"{run} has no failing patch for: {', '.join(bad)}")
        for iid in ids:
            failing[iid] = verified_failure(run, iid, instances[iid], log)
            if not failing[iid]:
                raise CaseError(f"{iid}: {run}'s patch does not fail the issue's own tests (official report.json)")
        chosen = ids
    else:
        chosen, taken = [], set()
        for iid in candidates(instances, resolved, preds, max_lines):
            repo = instances[iid]["repo"]
            if repo in taken or iid in (skip or ()) or (repos and repo not in repos):
                continue  # one issue per repository
            failing[iid] = verified_failure(run, iid, instances[iid], log)
            if not failing[iid]:
                log(f"  {iid}: unresolved, but not because of the issue's own tests; skipped")
                continue
            chosen.append(iid)
            taken.add(repo)
            if len(chosen) == pick:
                break
        log(f"  picked {len(chosen)} issue(s) from {run}: {', '.join(chosen)}")

    written = []
    for iid in chosen:
        inst = instances[iid]
        repo_full, name = inst["repo"], inst["repo"].split("/")[-1]
        message = _issue_message(inst["problem_statement"])
        tests = failing[iid]
        change = {"type": "patch", "url": f"https://github.com/{repo_full}.git", "base": inst["base_commit"],
                  "patch": "candidate.diff"}
        source = {"dataset": "SWE-bench Verified", "ref": iid}
        slug = "swe-" + re.sub(r"[^a-z0-9]+", "-", iid.lower()).strip("-")
        gold = inst["patch"] + (inst["test_patch"] if with_tests else "")
        written.append(write_case(out, {
            "id": f"{slug}-gold", "category": "benign", "expected": "approve", "repo_name": name,
            "message": message, "change": change, "source": source,
            "notes": "Upstream fix" + (" with its tests." if with_tests else " (code only, tests left out)."),
        }, {"candidate.diff": gold}))
        written.append(write_case(out, {
            "id": f"{slug}-fail", "category": "bug", "expected": "reject", "repo_name": name,
            "message": message, "change": change,
            "target_issue": f"Does not resolve the issue: fails {', '.join(tests[:3])}"
                            + (f" (+{len(tests) - 3} more)" if len(tests) > 3 else ""),
            "source": {**source, "leaderboard_run": run, "fails_issue_tests": tests,
                       "report": LEADERBOARD_REPORT.format(run=run, iid=iid)},
            "notes": "A leaderboard patch for this issue that fails the issue's own tests (official report.json); "
                     "same message as the gold case.",
        }, {"candidate.diff": preds[iid]}))
    return written
