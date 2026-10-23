"""Building the review prompt exactly as the attestor does.

The attestor (services/attestor/attestor/review.py + gitcheck.py) builds the prompt as
    build_prompt(template, render_markdown(build_pr(**pr_inputs(...)), template), decision_schema)
and this module calls the very same functions, so `review_input_hash` here equals the
one the attestor would sign for the same transition (tests/test_fidelity.py checks it).

Templates: any id in dosr_protocol.pr_format.TEMPLATES, or an experimental template
from a JSON file in experiments/templates/ (same fields as PRTemplate). Experimental
templates let us try prompt variants without touching the protocol package; a winner
gets added to pr_format.py under a new id in a separate PR.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from dosr_git import pr_inputs
from dosr_protocol.canonical import sha256_0x
from dosr_protocol.decision import DECISION_V1, build_prompt
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
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]
    path = Path(templates_dir) / f"{template_id}.json"
    if not path.is_file():
        raise PRFormatError(f"unknown template {template_id!r}: not in pr_format.TEMPLATES and no {path}")
    data = json.loads(path.read_text("utf-8"))
    if data.get("id") != template_id:
        raise PRFormatError(f"{path}: id must be {template_id!r}")
    if data["id"] in TEMPLATES:
        raise PRFormatError(f"{path}: id collides with a protocol template")
    data["context_globs"] = tuple(data.get("context_globs", ()))
    data["checklist"] = tuple(data.get("checklist", ()))
    return PRTemplate(**data)


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
