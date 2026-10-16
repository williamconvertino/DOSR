"""Client-side helpers for PR template files (format: dosr_protocol/templates.py).

Where template files are looked up by id (besides a direct file path):

    $DOSR_TEMPLATES        directories, separated by os.pathsep
    ~/.dosr/templates      your personal templates

A repository never reads these at review time: `dosr init --template` embeds the
template in the policy, so later edits to the file do not change existing repos.
"""

import copy
import os
from pathlib import Path

from dosr_protocol.pr_format import TEMPLATES, build_pr, get_template, render_markdown
from dosr_protocol.templates import (
    DEFAULT_SECTIONS,
    SECTION_TYPES,
    TEMPLATE_ID,
    VARIABLES,
    PRTemplate,
    TemplateError,
    find_templates,
    load_template_file,
    parse_template,
    parse_template_text,
    to_toml,
)

USER_TEMPLATES_DIR = Path.home() / ".dosr" / "templates"


def template_dirs(extra=()) -> list[Path]:
    dirs = [Path(d) for d in extra if d]
    dirs += [Path(d) for d in os.environ.get("DOSR_TEMPLATES", "").split(os.pathsep) if d.strip()]
    dirs.append(USER_TEMPLATES_DIR)
    out = []
    for d in dirs:
        if d.resolve() not in [o.resolve() for o in out]:
            out.append(d)
    return out


def available_templates(extra_dirs=()) -> list[dict]:
    """Built-ins, then template files found in `template_dirs()`, for pickers."""
    rows = [{"id": t.id, "description": t.description, "source": "built-in", "path": None, "hash": t.hash}
            for t in TEMPLATES.values()]
    for key, found in find_templates(*template_dirs(extra_dirs)).items():
        if "error" in found:
            rows.append({"id": key, "error": found["error"], "source": "file", "path": found["path"]})
        else:
            t = found["template"]
            rows.append({"id": t.id, "description": t.description, "source": "file", "path": found["path"],
                         "hash": t.hash})
    return rows


def resolve_template(spec: str, extra_dirs=()) -> tuple[PRTemplate, dict | None]:
    """A built-in id, a template file path, or the id of a file in template_dirs().
    Returns (template, definition to embed in the policy, or None for a built-in)."""
    if spec in TEMPLATES:
        return TEMPLATES[spec], None
    path = Path(spec)
    if path.suffix in (".toml", ".json") and path.is_file():
        t = load_template_file(path)
    else:
        found = find_templates(*template_dirs(extra_dirs)).get(spec)
        if not found:
            dirs = ", ".join(str(d) for d in template_dirs(extra_dirs))
            raise TemplateError(f"no template {spec!r}: not built in ({', '.join(TEMPLATES)}), not a file, "
                                f"and not found in {dirs}")
        t = found["template"]
    if t.id in TEMPLATES:
        raise TemplateError(f"{spec}: id {t.id!r} is a built-in template; give your template its own id")
    return t, t.definition


def policy_template(policy: dict) -> PRTemplate:
    rv = policy["review"]
    return get_template(rv["prompt_template"], rv.get("template"))


def scaffold(new_id: str, base: PRTemplate, description: str | None = None) -> str:
    """TOML for a new template copied from `base`, with every section written out and
    a comment header, ready to edit."""
    if not TEMPLATE_ID.match(new_id):
        raise TemplateError("template id must be 1-64 chars of lowercase letters, digits, '.', '_', '-'")
    d = copy.deepcopy(base.definition)
    d["id"] = new_id
    d["description"] = description or f"{base.description} (copied from {base.id})"
    # Write every section out with its options (defaults included), so they are easy to edit.
    d["sections"] = [
        {"type": s["type"], **{k: v for k, v in SECTION_TYPES[s["type"]].items() if v is not None},
         **{k: v for k, v in s.items() if k != "type"}}
        for s in d.get("sections", DEFAULT_SECTIONS)
    ]
    parse_template(d)  # still valid
    types = "\n".join(
        f"#   {k:<14} " + ", ".join(o for o in opts) for k, opts in SECTION_TYPES.items())
    variables = "\n".join(f"#   {k:<14} {', '.join(v)}" for k, v in VARIABLES.items())
    header = f"""\
# DOSR PR template (format reference: dosr_protocol/templates.py).
# Check it:   dosr template check {new_id}.toml --preview
# Use it:     dosr init PATH --template {new_id}.toml   (the template is embedded in the policy)
# Once a repository uses this id, never change the content under the same id: copy it to -v2.
#
# [context]   what is collected from Git (diff context lines, full files, extra files)
# [review]    the instructions + checklist the reviewer gets
# [[sections]] the PR layout, top to bottom. Section types and their options:
{types}
# body / heading / item / file_heading are format strings; {{name}} inserts a variable:
#   (all)          {', '.join(VARIABLES['*'])}
{chr(10).join(line for line in variables.splitlines() if not line.startswith('#   *'))}
# Write {{{{ and }}}} for literal braces.

"""
    return header + to_toml(parse_template(d))


SAMPLE_INPUTS = {
    "repo": {"name": "example-repo", "repo_id": "0x" + "5a" * 32},
    "parent_git_oid": "sha1:" + "1" * 40,
    "candidate_git_oid": "sha1:" + "2" * 40,
    "commits": [{"oid": "3f" * 20, "author": "Bob <bob@dosr.demo>", "subject": "Add power() and modulo()",
                 "body": "Adds two helpers requested in issue #12."}],
    "files": [{"path": "calculator.py", "status": "M", "additions": 8, "deletions": 0}],
    "diff_text": ("diff --git a/calculator.py b/calculator.py\n--- a/calculator.py\n+++ b/calculator.py\n"
                  "@@ -10,3 +10,11 @@ def multiply(a, b):\n     return a * b\n+\n+\n+def power(a, b):\n"
                  "+    return a ** b\n+\n+\n+def modulo(a, b):\n+    return a % b\n"),
    "full_files": {"calculator.py": "def add(a, b):\n    return a + b\n\n\ndef power(a, b):\n    return a ** b\n"},
    "context_files": {"README.md": "# example-repo\n\nA tiny calculator library.\n"},
}


def preview(template: PRTemplate) -> str:
    """The template applied to a small made-up change (what the reviewer would read)."""
    inputs = copy.deepcopy(SAMPLE_INPUTS)
    if not template.include_full_files:
        inputs["full_files"] = {}
    pr = build_pr(template=template, **inputs)
    return render_markdown(pr, template)


def check_text(text: str, name: str = "template") -> PRTemplate:
    return parse_template(parse_template_text(text, name), where=name)
