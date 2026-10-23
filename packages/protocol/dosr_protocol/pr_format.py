"""PR formats: what a proposed change looks like when packaged for review.

The repository policy names a template via `review.prompt_template` (a built-in id,
or a custom template embedded as `review.template`; see templates.py for the file
format). The template decides how much diff context, which full files, and which
extra repository files go into the PR, what the reviewer is told, and how the PR is
laid out as Markdown. This module is pure (no Git access) so the attestor can
rebuild the exact same PR from the Git bundle instead of trusting the client's copy.
"""

from dataclasses import replace
from fnmatch import fnmatch

from .canonical import canonical_json, sha256_0x
from .templates import (
    BUILTIN_TEMPLATES,
    DEFAULT_SECTIONS,
    SECTION_TYPES,
    PRTemplate,
    TemplateError,
    parse_template,
)

PR_FORMAT_VERSION = "dosr-pr-v1"

# Built-in templates (dosr_protocol/templates/*.toml), by id.
TEMPLATES: dict[str, PRTemplate] = BUILTIN_TEMPLATES


class PRFormatError(ValueError):
    pass


def get_template(template_id: str, definition: dict | None = None) -> PRTemplate:
    """A built-in template by id, or the custom one a policy embeds (`review.template`)."""
    if definition is not None:
        try:
            template = parse_template(definition, where="policy.review.template")
        except TemplateError as e:
            raise PRFormatError(str(e))
        if template.id != template_id:
            raise PRFormatError(f"policy.review.template has id {template.id!r}, but prompt_template is {template_id!r}")
        if template.id in TEMPLATES:
            raise PRFormatError(f"policy.review.template reuses the built-in id {template.id!r}; give it its own id")
        return template
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise PRFormatError(
            f"unknown PR template {template_id!r} (built in: {', '.join(sorted(TEMPLATES))}; "
            "a custom template must be embedded in the policy as review.template)"
        )


def template_for_policy(prompt_template: str, context_files=(), definition: dict | None = None) -> PRTemplate:
    """The template the policy names (built in, or embedded as `review.template`), plus
    the policy's own `review.context_files` globs. Client and attestor both build the
    PR (and prompt) from this, so a repo can require e.g. its architecture docs as
    context without a new template id."""
    template = get_template(prompt_template, definition)
    extra = tuple(g for g in context_files if g not in template.context_globs)
    return replace(template, context_globs=template.context_globs + extra) if extra else template


def select_context_files(template: PRTemplate, all_paths: list[str], changed: set[str]) -> list[str]:
    """Repository files (outside the change) that the template wants for context."""
    out = []
    for path in all_paths:
        name = path.rsplit("/", 1)[-1]
        if path in changed or path.startswith(".dosr/"):
            continue
        if any(fnmatch(name, g) or fnmatch(path, g) for g in template.context_globs):
            out.append(path)
    return sorted(out)


def check_limits(policy: dict, files: list[dict], diff_text: str) -> list[str]:
    review = policy["review"]
    problems = []
    if len(files) > review["max_changed_files"]:
        problems.append(
            f"{len(files)} changed files exceeds policy max_changed_files={review['max_changed_files']}"
        )
    patch_bytes = len(diff_text.encode("utf-8"))
    if patch_bytes > review["max_patch_bytes"]:
        problems.append(
            f"patch is {patch_bytes} bytes, exceeds policy max_patch_bytes={review['max_patch_bytes']}"
        )
    return problems


def build_pr(
    *,
    template: PRTemplate,
    repo: dict,
    parent_git_oid: str,
    candidate_git_oid: str,
    commits: list[dict],
    files: list[dict],
    diff_text: str,
    full_files: dict[str, str],
    context_files: dict[str, str],
) -> dict:
    """Assemble the PR document. `commits` items: oid, author, subject, body.
    `files` items: path, status, additions, deletions."""
    if not commits:
        raise PRFormatError("no commits between parent and candidate")
    title = commits[-1]["subject"] if len(commits) == 1 else (
        f"{commits[-1]['subject']} (+{len(commits) - 1} earlier commit(s))"
    )
    description = "\n\n".join(c["body"].strip() for c in commits if c.get("body", "").strip())

    pr = {
        "format": PR_FORMAT_VERSION,
        "template": template.id,
        "template_hash": template.hash,
        "repository": {"name": repo.get("name"), "repo_id": repo["repo_id"]},
        "transition": {"parent_git_oid": parent_git_oid, "candidate_git_oid": candidate_git_oid},
        "title": title,
        "description": description,
        "commits": commits,
        "stats": {
            "changed_files": len(files),
            "additions": sum(f.get("additions") or 0 for f in files),
            "deletions": sum(f.get("deletions") or 0 for f in files),
            "patch_bytes": len(diff_text.encode("utf-8")),
        },
        "files": files,
        "diff": diff_text,
        "full_files": full_files if template.include_full_files else {},
        "context_files": context_files,
        "review_instructions": template.instructions,
        "checklist": list(template.checklist),
    }
    pr["pr_hash"] = sha256_0x(canonical_json(pr))
    return pr


def text_metrics(text: str) -> dict:
    """Size of a text as a reviewer model would see it. `est_tokens` is the rough
    chars/4 rule of thumb for English + code; use a real tokenizer for billing."""
    return {
        "words": len(text.split()),
        "chars": len(text),
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "bytes": len(text.encode("utf-8")),
        "est_tokens": round(len(text) / 4),
    }


def pr_metrics(pr: dict, markdown: str) -> dict:
    """Totals for the rendered PR plus a per-section breakdown."""
    sections = {
        "diff": pr["diff"],
        "full_files": "\n".join(pr["full_files"].values()),
        "context_files": "\n".join(pr["context_files"].values()),
        "description": "\n".join(filter(None, [pr["title"], pr["description"]])),
        "instructions": "\n".join([pr["review_instructions"], *pr["checklist"]]),
    }
    return {
        "total": text_metrics(markdown),
        "sections": {name: text_metrics(text) for name, text in sections.items()},
        "token_estimate_method": "chars/4",
    }


def _vars(pr: dict) -> dict:
    s = pr["stats"]
    return {
        "title": pr["title"], "repo_name": pr["repository"]["name"], "repo_id": pr["repository"]["repo_id"],
        "repo_id_short": pr["repository"]["repo_id"][:18], "parent": pr["transition"]["parent_git_oid"],
        "candidate": pr["transition"]["candidate_git_oid"], "template_id": pr["template"],
        "template_hash": pr.get("template_hash", ""), "format": pr["format"], "pr_hash": pr.get("pr_hash", ""),
        "changed_files": s["changed_files"], "additions": s["additions"], "deletions": s["deletions"],
        "patch_bytes": s["patch_bytes"], "commit_count": len(pr["commits"]),
    }


def _fenced(text: str, fence: str) -> list[str]:
    return [f"```{fence}", text.rstrip("\n"), "```"]


def _render_section(sec: dict, pr: dict, g: dict) -> list[str]:
    """One layout section as lines; each non-empty section ends with a blank line."""
    kind = sec["type"]
    opt = {**SECTION_TYPES[kind], **sec}
    heading = opt.get("heading")
    head = [f"## {heading.format(**g)}", ""] if heading else []
    if kind == "text":
        return head + opt["body"].format(**g).rstrip("\n").split("\n") + [""]
    if kind == "description":
        return head + [pr["description"], ""] if pr["description"] else []
    if kind == "commits":
        items = [opt["item"].format_map({**g, "oid": c["oid"], "oid_short": c["oid"][:12], "subject": c["subject"],
                                         "author": c["author"]}) for c in pr["commits"]]
        return head + items + [""]
    if kind == "changed_files":
        # Item variables shadow the PR-wide ones of the same name (additions, deletions).
        items = [opt["item"].format_map({**g, "path": f["path"], "status": f["status"],
                                         "additions": f.get("additions") or 0, "deletions": f.get("deletions") or 0})
                 for f in pr["files"]]
        return head + items + [""]
    if kind == "instructions":
        out = head + [pr["review_instructions"], ""]
        if opt["include_checklist"] and pr["checklist"]:
            out += [opt["item"].format_map({**g, "item": i}) for i in pr["checklist"]] + [""]
        return out
    if kind == "diff":
        return head + _fenced(pr["diff"], opt["fence"]) + [""]
    if kind in ("full_files", "context_files"):
        files = pr[kind]
        if not files:
            return []
        out = list(head)
        for path, text in files.items():
            out += [opt["file_heading"].format_map({**g, "path": path}), ""] + _fenced(text, opt["fence"]) + [""]
        return out
    raise PRFormatError(f"unknown section type {kind!r}")


def render_markdown(pr: dict, template: PRTemplate | None = None) -> str:
    """The PR as Markdown (the body of the review prompt), laid out by the template's
    sections (or DEFAULT_SECTIONS)."""
    sections = template.sections if template is not None else DEFAULT_SECTIONS
    g = _vars(pr)
    lines: list[str] = []
    for sec in sections:
        lines += _render_section(sec, pr, g)
    return "\n".join(lines)
