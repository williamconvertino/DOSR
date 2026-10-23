"""Building the review prompt exactly as the attestor does.

The attestor (services/attestor/attestor/review.py + gitcheck.py) builds the prompt as
    build_prompt(template, render_markdown(build_pr(**pr_inputs(...)), template), decision_schema)
and this module calls the very same functions, so `review_input_hash` here equals the
one the attestor would sign for the same transition (tests/test_fidelity.py checks it).

Templates: any built-in id (dosr_protocol/templates/*.toml), or an experimental
template in experiments/templates/<id>.toml, in the same file format. Experimental
templates let us try prompt variants without touching the protocol package; a winner
gets added to pr_format.py under a new id in a separate PR.
"""

from dataclasses import dataclass
from pathlib import Path

from dosr_git import pr_inputs
from dosr_protocol.canonical import sha256_0x
from dosr_protocol.decision import DECISION_V1, build_prompt
from dosr_protocol.templates import TemplateError, load_template_file
from dosr_protocol.pr_format import (
    TEMPLATES,
    PRFormatError,
    PRTemplate,
    build_pr,
    check_limits,
    pr_metrics,
    render_markdown,
)

from .cases import Materialized

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

# PRs are not tied to a registered repository here; render_markdown shows its first 18 chars.
EVAL_REPO_ID = "0x" + "e7" * 32

# Limits of the llm-review preset, used only to flag cases the attestor would refuse
# before ever calling the model.
DEFAULT_LIMITS = {"review": {"max_changed_files": 20, "max_patch_bytes": 100_000}}


@dataclass
class ReviewInput:
    template: str
    prompt: str
    review_input_hash: str
    pr_hash: str
    metrics: dict
    limit_problems: list[str]


def load_template(template_id: str, templates_dir: Path = TEMPLATES_DIR) -> PRTemplate:
    """A built-in template by id, or experiments/templates/<id>.toml in the protocol's
    template format (dosr_protocol/templates.py), so a winner can be embedded in a
    policy as `review.template` unchanged."""
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]
    path = Path(templates_dir) / f"{template_id}.toml"
    if not path.is_file():
        raise PRFormatError(f"unknown template {template_id!r}: not built in and no {path}")
    try:
        template = load_template_file(path)
    except TemplateError as e:
        raise PRFormatError(str(e))
    if template.id != template_id:
        raise PRFormatError(f"{path}: id must be {template_id!r}")
    return template


def build_review_input(m: Materialized, template: PRTemplate, decision_schema: str = DECISION_V1,
                       limits: dict = DEFAULT_LIMITS) -> ReviewInput:
    inputs = pr_inputs(m.git, template, m.parent, m.candidate)
    pr = build_pr(
        template=template,
        repo={"name": m.case.repo_name, "repo_id": EVAL_REPO_ID},
        parent_git_oid=f"{m.object_format}:{m.parent}",
        candidate_git_oid=f"{m.object_format}:{m.candidate}",
        **inputs,
    )
    markdown = render_markdown(pr, template)
    prompt = build_prompt(template, markdown, decision_schema)
    return ReviewInput(
        template=template.id,
        prompt=prompt,
        review_input_hash=sha256_0x(prompt.encode("utf-8")),
        pr_hash=pr["pr_hash"],
        metrics=pr_metrics(pr, markdown),
        limit_problems=check_limits(limits, inputs["files"], inputs["diff_text"]),
    )
