"""`patch` cases, nested suites, grading, paper tables, and the importers' offline logic."""

import json

import pytest

from dosr_eval import importers
from dosr_eval.cases import CaseError, load_case, load_cases, materialize
from dosr_eval.metrics import load_rows, summarize, write_grade_sheet
from dosr_eval.paper import tables_tex
from dosr_eval.prompts import build_review_input, load_template
from conftest import CASES, git, write_case

PATCH = """diff --git a/q.py b/q.py
--- a/q.py
+++ b/q.py
@@ -1,2 +1,2 @@
 def q(name):
-    return f"SELECT * FROM t WHERE n = '{name}'"
+    return ("SELECT * FROM t WHERE n = ?", (name,))
"""


@pytest.fixture
def upstream(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    (up / "q.py").write_text("def q(name):\n    return f\"SELECT * FROM t WHERE n = '{name}'\"\n")
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "base")
    git(up, "config", "uploadpack.allowAnySHA1InWant", "true")
    return up, git(up, "rev-parse", "HEAD")


def _patch_case(root, up, base, patch, message="Use a bound parameter"):
    return write_case(root, {
        "id": "p", "category": "benign", "expected": "approve", "message": message,
        "change": {"type": "patch", "url": up.as_uri(), "base": base, "patch": "candidate.diff"},
    }, {"candidate.diff": patch})


def test_patch_case_applies_on_base(tmp_path, upstream):
    up, base = upstream
    m = materialize(load_case(_patch_case(tmp_path / "cases", up, base, PATCH)), tmp_path / "w", tmp_path / "g")
    assert m.parent == base and m.candidate != base
    ri = build_review_input(m, load_template("security-sensitive-review-v2"))
    assert "Use a bound parameter" in ri.prompt and '+    return ("SELECT * FROM t WHERE n = ?"' in ri.prompt
    again = materialize(load_case(tmp_path / "cases" / "p"), tmp_path / "w2", tmp_path / "g")
    assert again.candidate == m.candidate  # reproducible


def test_patch_that_does_not_apply_is_an_error(tmp_path, upstream):
    up, base = upstream
    with pytest.raises(CaseError, match="does not apply"):
        materialize(load_case(_patch_case(tmp_path / "cases", up, base, PATCH.replace("t WHERE", "x WHERE"))),
                    tmp_path / "w", tmp_path / "g")


def test_cases_load_from_nested_suites(tmp_path):
    write_case(tmp_path / "suite-a", {"id": "a", "category": "benign", "expected": "approve",
                                      "change": {"type": "files", "base": "b"}})
    write_case(tmp_path / "suite-b" / "deeper", {"id": "b", "category": "bug", "expected": "reject",
                                                 "change": {"type": "files", "base": "b"}})
    assert [c.id for c in load_cases(tmp_path)] == ["a", "b"]
    assert [c.id for c in load_cases(tmp_path / "suite-a")] == ["a"]


def test_pilot_suite_is_well_formed():
    cases = load_cases(CASES / "pilot-v1")
    assert len(cases) == 20
    assert sum(c.expected == "approve" for c in cases) == 8
    for c in cases:
        if c.expected == "reject":
            assert c.target_issue, c.id
        if c.change["type"] == "patch":
            assert (c.dir / c.change["patch"]).read_text().startswith("diff --git"), c.id


def _row(case, expected, approved, rep=0):
    return {"run_id": "r", "case": case, "dataset": "SEVRA", "category": "vuln" if expected == "reject" else "benign",
            "expected": expected, "template": "security-sensitive-review-v2", "model": "m", "rep": rep,
            "approved": approved, "summary": "s", "issues": ["i"], "parse_error": None, "target_issue": "t",
            "cost": 0.01, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


def test_grading_roundtrip_adds_diagnosis(tmp_path):
    rows = [_row("x", "reject", False), _row("y", "reject", False), _row("z", "reject", True),
            _row("w", "approve", True)]
    results = tmp_path / "r.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in rows))
    sheet = tmp_path / "g.csv"
    assert write_grade_sheet(rows, sheet) == 2  # only correct rejections
    text = sheet.read_text().splitlines()
    sheet.write_text("\n".join([text[0], text[1] + "y", text[2] + "n"]) + "\n")  # found_target is last
    s = summarize(load_rows(results, sheet))
    assert s["diagnosis"] == 0.5 and s["miss"] == pytest.approx(1 / 3)


def test_tables_tex_without_matplotlib():
    tex = tables_tex([_row("x", "reject", True), _row("y", "reject", False), _row("w", "approve", True)],
                     currency="USD")
    assert r"\toprule" in tex and "A: verdict + reason" in tex and "CVE fix reverted" in tex
    assert "0.02" in tex  # cost per accepted bad PR: 0.01 / 50% miss


def test_sevra_fix_commits_and_messages():
    adv = {"references": ["https://github.com/a/b/pull/1", "https://github.com/a/b/commit/" + "1" * 40,
                          "https://github.com/a/b/commit/" + "1" * 40]}
    assert importers.fix_commits(adv) == [("a/b", "1" * 40)]
    assert importers._neutral_message(["x.py"]) == "Update x.py"
    assert importers._neutral_message(["x.py", "y.py", "z.py"]) == "Update x.py and 2 other files"


def test_swebench_candidates_pick_real_failing_attempts():
    gold = "diff --git a/pkg/m.py b/pkg/m.py\n--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1 +1 @@\n-a\n+b\n"
    instances = {i: {"repo": "o/r", "patch": gold} for i in ("i1", "i2", "i3", "i4", "i5")}
    preds = {
        "i1": gold,                                                  # resolved -> skipped
        "i2": "",                                                    # empty -> skipped
        "i3": gold.replace("pkg/m.py", "other.py"),                  # different file -> skipped
        "i4": gold + gold.replace("pkg/m.py", "reproduce_bug.py"),   # scratch file -> skipped
        "i5": gold.replace("+b", "+c"),                              # failing attempt -> kept
    }
    assert importers.candidates(instances, {"i1"}, preds, max_lines=60) == ["i5"]


def test_issue_message_has_title_and_body():
    msg = importers._issue_message("Wrong legend values\n\nDetails here.")
    assert msg.startswith("Fix: Wrong legend values\n\nFixes the following issue:")


def test_report_can_exclude_a_mislabelled_case(tmp_path, capsys):
    from dosr_eval.__main__ import main

    results = tmp_path / "r.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in [_row("bad-label", "reject", True), _row("ok", "reject", False)]))
    assert main(["report", str(results), "--exclude", "bad-label"]) == 0
    out = capsys.readouterr().out
    assert "bad-label" not in out and "| 1 | 0 | 100% |" in out
    assert main(["report", str(results), "--exclude", "no-such-case"]) == 1


def test_issue_test_failures_needs_the_issues_own_tests():
    test_patch = ("diff --git a/tests/validators/valid_urls.txt b/tests/validators/valid_urls.txt\n"
                  "diff --git a/astropy/timeseries/tests/test_sampled.py b/astropy/timeseries/tests/test_sampled.py\n")
    def report(failed):
        return {"x": {"tests_status": {"FAIL_TO_PASS": {"failure": failed, "success": []}}}}
    env_only = ["test_PasswordChangeDoneView (auth_tests.test_templates.AuthTemplateTests)"]
    assert importers.issue_test_failures(report(env_only), test_patch) == []
    django_own = "test_url_validator (validators.tests.TestValidators)"
    pytest_own = "astropy/timeseries/tests/test_sampled.py::test_required_columns"
    assert importers.issue_test_failures(report(env_only + [django_own, pytest_own]), test_patch) == \
        [django_own, pytest_own]
    assert importers.issue_test_failures({}, test_patch) == []


def test_report_combines_runs(tmp_path, capsys):
    from dosr_eval.__main__ import main

    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(json.dumps(_row("x", "reject", False)))
    b.write_text(json.dumps({**_row("y", "reject", True), "run_id": "r2"}))
    assert main(["report", str(a), str(b)]) == 0
    assert "| 2 | 0 | 50% |" in capsys.readouterr().out
