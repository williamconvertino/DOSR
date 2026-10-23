import pytest
from dosr_protocol.pr_format import get_template

from dosr_eval.cases import CaseError, load_case, load_cases, materialize
from dosr_eval.prompts import build_review_input, load_template
from conftest import CASES, git, write_case


def test_shipped_cases_load_and_build(tmp_path):
    cases = load_cases(CASES)
    assert cases
    template = get_template("security-sensitive-review-v2")
    for case in cases:
        if case.change["type"] in ("git", "patch"):
            continue  # needs the network
        ri = build_review_input(materialize(case, tmp_path / "w", tmp_path / "g"), template)
        assert ri.prompt and not ri.limit_problems, case.id


def test_target_issue_is_withheld_from_the_prompt(tmp_path):
    template = get_template("security-sensitive-review-v2")
    for case in load_cases(CASES):
        if case.target_issue and case.change["type"] not in ("git", "patch"):
            ri = build_review_input(materialize(case, tmp_path / "w", tmp_path / "g"), template)
            assert case.target_issue not in ri.prompt


def test_prompts_are_reproducible(tmp_path):
    (case,) = load_cases(CASES, ["demo-calc-add-power"])
    template = get_template("standard-code-review-v1")
    a = build_review_input(materialize(case, tmp_path / "a", tmp_path / "g"), template)
    b = build_review_input(materialize(case, tmp_path / "b", tmp_path / "g"), template)
    assert a.review_input_hash == b.review_input_hash


def test_message_override_reaches_the_prompt(tmp_path):
    (case,) = load_cases(CASES, ["demo-calc-divide-deceptive"])
    ri = build_review_input(materialize(case, tmp_path / "w", tmp_path / "g"),
                            get_template("security-sensitive-review-v2"))
    assert "No changes to existing operations." in ri.prompt
    assert "return 0" in ri.prompt


def test_files_case_with_delete(tmp_path):
    d = write_case(tmp_path / "cases", {
        "id": "del", "category": "bug", "expected": "reject", "message": "Remove tests",
        "change": {"type": "files", "base": "base", "delete": ["tests/test_a.py"]},
    }, {"base/a.py": "x = 1\n", "base/tests/test_a.py": "def test_a(): pass\n"})
    m = materialize(load_case(d), tmp_path / "w", tmp_path / "g")
    files = m.git.changed_files(m.parent, m.candidate)
    assert files == [{"path": "tests/test_a.py", "status": "D", "additions": 0, "deletions": 1,
                      "binary": False}]


@pytest.fixture
def upstream(tmp_path):
    """A local 'public' repo with a vulnerable commit followed by its fix."""
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    (up / "q.py").write_text("def q(name):\n    return f\"SELECT * FROM t WHERE n = '{name}'\"\n")
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "vulnerable")
    vuln = git(up, "rev-parse", "HEAD")
    (up / "q.py").write_text("def q(name):\n    return (\"SELECT * FROM t WHERE n = ?\", (name,))\n")
    git(up, "commit", "-qam", "Fix SQL injection in q()")
    fix = git(up, "rev-parse", "HEAD")
    return up, vuln, fix


def test_git_case_forward(tmp_path, upstream):
    up, vuln, fix = upstream
    d = write_case(tmp_path / "cases", {
        "id": "fix", "category": "vuln", "expected": "approve",
        "change": {"type": "git", "url": up.as_uri(), "base": vuln, "head": fix},
    })
    m = materialize(load_case(d), tmp_path / "w", tmp_path / "g")
    assert (m.parent, m.candidate) == (vuln, fix)
    ri = build_review_input(m, load_template("security-sensitive-review-v2"))
    assert "Fix SQL injection in q()" in ri.prompt


def test_git_case_reverse_with_deceptive_message(tmp_path, upstream):
    up, vuln, fix = upstream
    d = write_case(tmp_path / "cases", {
        "id": "rev", "category": "deceptive", "expected": "reject",
        "message": "Simplify q() return type",
        "change": {"type": "git", "url": up.as_uri(), "base": vuln, "head": fix, "reverse": True},
    })
    m = materialize(load_case(d), tmp_path / "w", tmp_path / "g")
    assert m.parent == fix
    assert m.git.run("rev-parse", f"{m.candidate}^{{tree}}").strip() == \
        m.git.run("rev-parse", f"{vuln}^{{tree}}").strip()
    ri = build_review_input(m, load_template("security-sensitive-review-v2"))
    assert "Simplify q() return type" in ri.prompt
    assert "+    return f\"SELECT" in ri.prompt


def test_bad_case_is_rejected(tmp_path):
    d = write_case(tmp_path / "cases", {"id": "x", "category": "nope", "expected": "approve",
                                        "change": {"type": "files", "base": "b"}})
    with pytest.raises(CaseError):
        load_case(d)


def test_experimental_template_loads():
    t = load_template("exp-example-evidence-v0")
    assert t.include_full_files and "path:line" in t.instructions


def test_pilot_templates_share_template_a_context():
    """B and C differ from A only in their instructions, so the comparison isolates them."""
    a = load_template("security-sensitive-review-v2")
    for tid in ("exp-evidence-v1", "exp-blocking-only-v1"):
        t = load_template(tid)
        assert (t.diff_context_lines, t.include_full_files, t.context_globs, t.max_file_bytes) == \
            (a.diff_context_lines, a.include_full_files, a.context_globs, a.max_file_bytes), tid
        assert t.instructions != a.instructions
