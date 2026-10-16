import copy
from pathlib import Path

import pytest

from dosr_protocol.policy import PolicyError, build_policy, validate_policy
from dosr_protocol.pr_format import TEMPLATES, PRFormatError, build_pr, render_markdown, template_for_policy
from dosr_protocol.templates import (
    DEFAULT_SECTIONS,
    TemplateError,
    load_template_file,
    parse_template,
    parse_template_text,
    template_hash,
    to_toml,
)

DEMO_TEMPLATE = Path(__file__).resolve().parents[3] / "demo" / "templates" / "team-review-v1.toml"

PR_INPUTS = dict(
    repo={"name": "r", "repo_id": "0x" + "ab" * 32}, parent_git_oid="sha1:" + "1" * 40,
    candidate_git_oid="sha1:" + "2" * 40,
    commits=[{"oid": "3" * 40, "author": "A", "subject": "Add x", "body": "Why."}],
    files=[{"path": "x.py", "status": "M", "additions": 2, "deletions": 1}],
    diff_text="+x\n", full_files={"x.py": "x = 1\n"}, context_files={"docs/RULES.md": "no eval\n"},
)


def minimal(**over):
    d = {
        "format": "dosr-pr-template-v1", "id": "mine-v1", "description": "Mine",
        "context": {"diff_context_lines": 3, "include_full_files": False, "max_file_bytes": 1000},
        "review": {"instructions": "Be careful."},
    }
    d.update(over)
    return d


def test_builtins_are_files_with_the_default_layout():
    assert set(TEMPLATES) == {"standard-code-review-v1", "security-sensitive-review-v1", "security-sensitive-review-v2"}
    for t in TEMPLATES.values():
        assert t.builtin and t.sections == DEFAULT_SECTIONS and t.hash.startswith("0x")
        # TOML round trip keeps the definition (and so the hash).
        assert parse_template(parse_template_text(to_toml(t))).definition == t.definition


def test_pr_records_the_template_hash():
    t = TEMPLATES["standard-code-review-v1"]
    pr = build_pr(template=t, **PR_INPUTS)
    assert pr["template_hash"] == t.hash == template_hash(t.definition)


def test_custom_layout_renders_in_order_with_variables():
    t = load_template_file(DEMO_TEMPLATE)
    assert t.id == "team-review-v1" and "docs/*.md" in t.context_globs
    pr = build_pr(template=t, **PR_INPUTS)
    md = render_markdown(pr, t)
    order = [md.index(h) for h in ("# Pull request: Add x", "## How to review", "## Why (from the commit messages)",
                                   "## Commits", "## Files", "## Patch", "## Project docs and rules")]
    assert order == sorted(order)
    assert "- Add x (A, `333333333333`)" in md and "- x.py: M, +2/-1" in md
    assert "### Rules file: docs/RULES.md" in md
    assert "## Full text of the changed files" in md and "### x.py" in md
    assert "- [ ]" not in md  # its checklist item format is "- {item}"
    assert md != render_markdown(pr)  # differs from the default layout


def test_text_section_and_literal_braces():
    t = parse_template(minimal(sections=[{"type": "text", "body": "{{literal}} {title} by {commit_count}"},
                                         {"type": "diff", "heading": "", "fence": ""}]))
    md = render_markdown(build_pr(template=t, **PR_INPUTS), t)
    assert md == "{literal} Add x by 1\n\n```\n+x\n```\n"


@pytest.mark.parametrize("over, msg", [
    ({"format": "v0"}, "format"),
    ({"id": "Has Caps"}, "id"),
    ({"surprise": 1}, "unknown key"),
    ({"review": {"instructions": ""}}, "instructions"),
    ({"review": {"instructions": "x", "checklist": "nope"}}, "checklist"),
    ({"context": {"diff_context_lines": -1, "include_full_files": True, "max_file_bytes": 1}}, "diff_context_lines"),
    ({"context": {"diff_context_lines": 3, "include_full_files": True, "max_file_bytes": 1, "files": ["../x"]}}, "files"),
    ({"sections": [{"type": "video"}]}, "type"),
    ({"sections": [{"type": "text"}]}, "body"),
    ({"sections": [{"type": "diff", "colour": "red"}]}, "unknown key"),
    ({"sections": [{"type": "text", "body": "{secret}"}]}, "unknown variable {secret}"),
    ({"sections": [{"type": "commits", "item": "{path}"}]}, "unknown variable {path}"),
    ({"sections": [{"type": "text", "body": "{title!r}"}]}, "not supported"),
    ({"sections": [{"type": "text", "body": "{title"}]}, "bad format string"),
    ({"sections": [{"type": "diff", "fence": "a b"}]}, "fence"),
    ({"sections": []}, "sections"),
])
def test_invalid_templates_are_rejected(over, msg):
    with pytest.raises(TemplateError, match=msg.replace("{", r"\{").replace("}", r"\}")):
        parse_template(minimal(**over))


def test_policy_embeds_custom_template():
    definition = load_template_file(DEMO_TEMPLATE).definition
    p = build_policy("standard", template=definition)
    assert p["review"]["prompt_template"] == "team-review-v1" and p["review"]["template"] == definition
    validate_policy(p)
    t = template_for_policy("team-review-v1", ["extra/*.md"], p["review"]["template"])
    assert t.context_globs[-1] == "extra/*.md" and t.sections[0]["type"] == "text"


def test_policy_rejects_bad_or_mismatched_templates():
    definition = load_template_file(DEMO_TEMPLATE).definition
    p = build_policy("standard", template=definition)
    p["review"]["prompt_template"] = "other-v1"
    with pytest.raises(PolicyError, match="has id"):
        validate_policy(p)
    clash = dict(copy.deepcopy(definition), id="standard-code-review-v1")
    with pytest.raises(PolicyError, match="built-in"):
        build_policy("standard", template=clash)
    with pytest.raises(PolicyError, match="unknown PR template"):
        build_policy("standard", prompt_template="nope-v1")
    with pytest.raises(PRFormatError):
        template_for_policy("x-v1", (), minimal(id="x-v1", sections=[{"type": "nope"}]))
