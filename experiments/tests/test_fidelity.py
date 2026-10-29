"""The harness must send the model exactly what the attestor would."""

from dosr_git import create_bundle
from dosr_protocol.canonical import sha256_0x
from dosr_protocol.decision import DECISION_V1, build_prompt
from dosr_protocol.pr_format import get_template, render_markdown

from dosr_eval.cases import load_cases, materialize
from dosr_eval.prompts import EVAL_REPO_ID, build_review_input
from dosr_eval.reviewers import attestor_reviewers
from conftest import CASES


def test_prompt_matches_attestor(tmp_path):
    attestor_reviewers()  # puts services/attestor on sys.path
    from attestor import gitcheck

    template = get_template("security-sensitive-review-v2")
    for case in load_cases(CASES):
        if case.change["type"] in ("git", "patch"):
            continue
        m = materialize(case, tmp_path / "work", tmp_path / "git")
        ours = build_review_input(m, template)

        bundle = create_bundle(m.git, m.candidate, tmp_path / f"{case.id}.bundle", parent=m.parent)
        transition = gitcheck.open_transition(
            gitcheck.ObjectCache(tmp_path / "attestor-cache", 30), EVAL_REPO_ID, "sha1",
            (tmp_path / f"{case.id}.bundle").read_bytes(), m.parent, m.candidate, template, case.repo_name)
        theirs = build_prompt(template, render_markdown(transition.pr, template), DECISION_V1)

        assert bundle.head_oid == m.candidate
        assert ours.pr_hash == transition.pr["pr_hash"], case.id
        assert ours.review_input_hash == sha256_0x(theirs.encode("utf-8")), case.id
