"""PR template files: how a proposed change is packaged into a PR (and so into the prompt).

A template is a small TOML (or JSON) file. Built-in templates live in
`dosr_protocol/templates/*.toml`; anyone can write their own. Start from a copy with
`dosr template new`, check it with `dosr template check`, and use it with
`dosr init --template FILE`.

    format = "dosr-pr-template-v1"
    id = "my-team-review-v1"           # lowercase letters, digits, . _ - ; put a version in it
    description = "One line shown in pickers"

    [context]                          # what is collected from Git
    diff_context_lines = 3             # unified-diff context lines
    include_full_files = true          # full text of every changed file
    max_file_bytes = 64000             # larger files are left out
    files = ["README*", "docs/*.md"]   # extra repository files (name or path globs)

    [review]                           # what the reviewer is told
    instructions = "Approve only if ..."
    checklist = ["No secrets", "Tests updated"]

    [[sections]]                       # the PR layout, in order (optional: DEFAULT_SECTIONS)
    type = "text"
    body = "# {title}"
    [[sections]]
    type = "diff"

Section types (SECTION_TYPES) and their options:

    text           body (required), heading
    description    heading                       (left out if no commit has a body)
    commits        heading, item
    changed_files  heading, item
    instructions   heading, item, include_checklist
    diff           heading, fence
    full_files     heading, file_heading, fence  (left out if empty)
    context_files  heading, file_heading, fence  (left out if empty)

`body`, `heading`, `item` and `file_heading` are format strings: `{name}` inserts a
value (see VARIABLES), `{{` and `}}` are literal braces. Only the listed names exist;
anything else is rejected when the template is loaded, so a typo fails early, not in
the middle of a review.

How a template is bound to a repository: the policy names it in
`review.prompt_template`. A built-in is named by id. A custom template is embedded in
the policy as `review.template` (the parsed file), so its exact content is covered by
the policy hash registered at genesis, and the attestor gets it with every request
without any shared registry. A template's content must never change under the same id:
copy it to a new id (`...-v2`) instead. Every PR records `template_hash`, the sha256
of the template's canonical JSON.

To change what templates can express, add a section type (here and in
pr_format._render_section) or bump TEMPLATE_FORMAT and handle both versions.
"""

import json
import re
import string
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .canonical import canonical_json, sha256_0x

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as _toml  # type: ignore[no-redef]

TEMPLATE_FORMAT = "dosr-pr-template-v1"
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_TEXT = 20_000
MAX_GLOBS = 50
MAX_SECTIONS = 40

# Variables every format string can use.
GLOBAL_VARIABLES = {
    "title", "repo_name", "repo_id", "repo_id_short", "parent", "candidate", "template_id",
    "template_hash", "format", "pr_hash", "changed_files", "additions", "deletions",
    "patch_bytes", "commit_count",
}
# Extra variables for a section's `item` / `file_heading`.
ITEM_VARIABLES = {
    "commits": {"oid", "oid_short", "subject", "author"},
    "changed_files": {"path", "status", "additions", "deletions"},
    "instructions": {"item"},
    "full_files": {"path"},
    "context_files": {"path"},
}
VARIABLES = {"*": sorted(GLOBAL_VARIABLES), **{k: sorted(v) for k, v in ITEM_VARIABLES.items()}}

# type -> {option: default}. `None` = optional with no default.
SECTION_TYPES: dict[str, dict] = {
    "text": {"body": None, "heading": None},
    "description": {"heading": "Description"},
    "commits": {"heading": "Commits", "item": "- `{oid_short}` {subject} — {author}"},
    "changed_files": {"heading": "Changed files", "item": "- `{status}` {path} (+{additions}/-{deletions})"},
    "instructions": {"heading": "Review instructions", "item": "- [ ] {item}", "include_checklist": True},
    "diff": {"heading": "Diff", "fence": "diff"},
    "full_files": {"heading": "Full changed files", "file_heading": "### {path}", "fence": ""},
    "context_files": {"heading": "Context files", "file_heading": "### {path}", "fence": ""},
}

HEADER_BODY = """\
# {title}

- Repository: `{repo_name}` (`{repo_id_short}…`)
- Parent: `{parent}`
- Candidate: `{candidate}`
- Template: `{template_id}` · Format: `{format}`
- Stats: {changed_files} file(s), +{additions} / -{deletions}, {patch_bytes} patch bytes
- PR hash: `{pr_hash}`"""

# The layout used when a template has no [[sections]].
DEFAULT_SECTIONS: tuple[dict, ...] = (
    {"type": "text", "body": HEADER_BODY},
    {"type": "description"},
    {"type": "commits"},
    {"type": "changed_files"},
    {"type": "instructions"},
    {"type": "diff"},
    {"type": "full_files"},
    {"type": "context_files"},
)


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class PRTemplate:
    id: str
    description: str
    diff_context_lines: int
    include_full_files: bool
    context_globs: tuple[str, ...]
    max_file_bytes: int
    instructions: str
    checklist: tuple[str, ...] = ()
    sections: tuple[dict, ...] = DEFAULT_SECTIONS
    # The parsed file this template came from (what a policy embeds and what is hashed).
    definition: dict = field(default_factory=dict, compare=False, repr=False)
    builtin: bool = False

    @property
    def hash(self) -> str:
        return template_hash(self.definition)


def template_hash(definition: dict) -> str:
    return sha256_0x(canonical_json(definition))


# ------------------------------------------------------------------ validation


def _fail(where: str, msg: str):
    raise TemplateError(f"{where}: {msg}")


def _check_keys(obj, allowed: set, required: set, where: str) -> None:
    if not isinstance(obj, dict):
        _fail(where, "must be a table/object")
    missing, unknown = required - obj.keys(), obj.keys() - allowed
    if missing:
        _fail(where, f"missing {', '.join(sorted(missing))}")
    if unknown:
        _fail(where, f"unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(allowed))})")


def _check_text(value, where: str, *, allow_empty=False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > MAX_TEXT:
        _fail(where, f"must be a {'' if allow_empty else 'non-empty '}string of at most {MAX_TEXT} characters")
    return value


def _check_int(value, lo: int, hi: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        _fail(where, f"must be an integer from {lo} to {hi}")
    return value


def _check_format_string(text: str, allowed: set, where: str) -> None:
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError as e:
        _fail(where, f"bad format string ({e}); write {{{{ and }}}} for literal braces")
    for _, name, spec, conversion in parts:
        if name is None:
            continue
        if name not in allowed:
            _fail(where, f"unknown variable {{{name}}} (available: {', '.join(sorted(allowed))})")
        if spec or conversion:
            _fail(where, f"{{{name}}}: format specs and conversions are not supported")


def check_globs(globs, where: str) -> tuple[str, ...]:
    if not isinstance(globs, list) or len(globs) > MAX_GLOBS or not all(
        isinstance(g, str) and g.strip() and not g.startswith(("/", "../")) and ".." not in g.split("/")
        for g in globs
    ):
        _fail(where, f"must be a list of at most {MAX_GLOBS} relative paths/globs inside the repository")
    return tuple(globs)


def _check_section(sec, i: int) -> dict:
    where = f"sections[{i}]"
    if not isinstance(sec, dict) or sec.get("type") not in SECTION_TYPES:
        _fail(where, f"`type` must be one of {', '.join(SECTION_TYPES)}")
    kind = sec["type"]
    options = SECTION_TYPES[kind]
    _check_keys(sec, {"type", *options}, {"type", *(["body"] if kind == "text" else [])}, f"{where} ({kind})")
    item_vars = GLOBAL_VARIABLES | ITEM_VARIABLES.get(kind, set())
    for key, value in sec.items():
        if key == "type":
            continue
        if key == "include_checklist":
            if not isinstance(value, bool):
                _fail(f"{where}.{key}", "must be true or false")
            continue
        _check_text(value, f"{where}.{key}", allow_empty=key in ("fence", "heading"))
        if key == "fence" and not re.fullmatch(r"[A-Za-z0-9_+.-]*", value):
            _fail(f"{where}.fence", "must be a code-fence language like 'diff', 'python' or empty")
        if key in ("body", "heading"):
            _check_format_string(value, GLOBAL_VARIABLES, f"{where}.{key}")
        if key in ("item", "file_heading"):
            _check_format_string(value, item_vars, f"{where}.{key}")
    return dict(sec)


def parse_template(definition: dict, *, where: str = "template", builtin: bool = False) -> PRTemplate:
    """Validate a template definition (a parsed file, or `policy.review.template`)."""
    try:
        json.dumps(definition, allow_nan=False)
    except (TypeError, ValueError):
        _fail(where, "may only contain strings, numbers, booleans, lists and tables (no dates)")
    _check_keys(definition, {"format", "id", "description", "context", "review", "sections"},
                {"format", "id", "description", "context", "review"}, where)
    if definition["format"] != TEMPLATE_FORMAT:
        _fail(f"{where}.format", f"must be {TEMPLATE_FORMAT!r}")
    tid = definition["id"]
    if not isinstance(tid, str) or not TEMPLATE_ID.match(tid):
        _fail(f"{where}.id", "must be 1-64 chars of lowercase letters, digits, '.', '_', '-' (e.g. 'team-review-v1')")
    description = _check_text(definition["description"], f"{where}.description")
    ctx, rv = definition["context"], definition["review"]
    _check_keys(ctx, {"diff_context_lines", "include_full_files", "max_file_bytes", "files"},
                {"diff_context_lines", "include_full_files", "max_file_bytes"}, f"{where}.context")
    if not isinstance(ctx["include_full_files"], bool):
        _fail(f"{where}.context.include_full_files", "must be true or false")
    _check_keys(rv, {"instructions", "checklist"}, {"instructions"}, f"{where}.review")
    checklist = rv.get("checklist", [])
    if not isinstance(checklist, list) or len(checklist) > 50:
        _fail(f"{where}.review.checklist", "must be a list of at most 50 strings")
    for i, item in enumerate(checklist):
        _check_text(item, f"{where}.review.checklist[{i}]")
    sections = definition.get("sections")
    if sections is None:
        parsed_sections = DEFAULT_SECTIONS
    else:
        if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
            _fail(f"{where}.sections", f"must be a list of 1..{MAX_SECTIONS} sections")
        parsed_sections = tuple(_check_section(s, i) for i, s in enumerate(sections))
    return PRTemplate(
        id=tid,
        description=description,
        diff_context_lines=_check_int(ctx["diff_context_lines"], 0, 1000, f"{where}.context.diff_context_lines"),
        include_full_files=ctx["include_full_files"],
        context_globs=check_globs(ctx.get("files", []), f"{where}.context.files"),
        max_file_bytes=_check_int(ctx["max_file_bytes"], 1, 10_000_000, f"{where}.context.max_file_bytes"),
        instructions=_check_text(rv["instructions"], f"{where}.review.instructions"),
        checklist=tuple(checklist),
        sections=parsed_sections,
        definition=definition,
        builtin=builtin,
    )


# ------------------------------------------------------------------ files


def parse_template_text(text: str, name: str = "template") -> dict:
    """TOML or JSON text -> definition dict (not yet validated)."""
    stripped = text.lstrip()
    try:
        if stripped.startswith("{"):
            return json.loads(text)
        return _toml.loads(text)
    except (ValueError, _toml.TOMLDecodeError) as e:
        raise TemplateError(f"{name}: cannot parse ({e})")


def load_template_file(path) -> PRTemplate:
    path = Path(path)
    try:
        text = path.read_text("utf-8")
    except OSError as e:
        raise TemplateError(f"cannot read template {path}: {e}")
    return parse_template(parse_template_text(text, path.name), where=path.name)


def _load_builtins() -> dict[str, PRTemplate]:
    out = {}
    for entry in sorted(resources.files("dosr_protocol").joinpath("templates").iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".toml"):
            t = parse_template(parse_template_text(entry.read_text("utf-8"), entry.name),
                               where=entry.name, builtin=True)
            if t.id in out:
                raise TemplateError(f"duplicate built-in template id {t.id!r}")
            out[t.id] = t
    return out


BUILTIN_TEMPLATES: dict[str, PRTemplate] = _load_builtins()


def find_templates(*dirs) -> dict[str, dict]:
    """Template files in `dirs` (*.toml, *.json): id -> {template, path} or {error, path}.
    For pickers; a repository only ever uses the copy embedded in its policy."""
    found: dict[str, dict] = {}
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        for path in sorted(Path(d).iterdir()):
            if path.suffix not in (".toml", ".json") or not path.is_file():
                continue
            try:
                t = load_template_file(path)
                if t.id in BUILTIN_TEMPLATES:
                    raise TemplateError(f"{path.name}: id {t.id!r} is a built-in template; pick another id")
                found.setdefault(t.id, {"template": t, "path": str(path)})
            except TemplateError as e:
                found[f"!{path.name}"] = {"error": str(e), "path": str(path)}
    return found


def to_toml(t: PRTemplate) -> str:
    """A template as TOML text (for `dosr template new` / show)."""
    d = t.definition

    def s(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str):
            # Multi-line literal string: the newline after ''' is dropped, nothing is escaped.
            if "\n" in v and "'''" not in v and "\r" not in v:
                return f"'''\n{v}'''"
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            if not v:
                return "[]"
            return "[\n" + "".join(f"    {s(x)},\n" for x in v) + "]"
        raise TypeError(type(v))

    lines = [f"format = {s(d['format'])}", f"id = {s(d['id'])}", f"description = {s(d['description'])}", "",
             "[context]"]
    lines += [f"{k} = {s(v)}" for k, v in d["context"].items()]
    lines += ["", "[review]"] + [f"{k} = {s(v)}" for k, v in d["review"].items()]
    for sec in d.get("sections", []):
        lines += ["", "[[sections]]"] + [f"{k} = {s(v)}" for k, v in sec.items()]
    return "\n".join(lines) + "\n"
