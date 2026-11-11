"""`--context callers`: call sites of the changed code, built deterministically from Git."""

import json

import pytest
from dosr_protocol.pr_format import get_template

from dosr_eval import context
from dosr_eval.cases import load_case, materialize
from dosr_eval.context import changed_symbols, definitions, parse_diff, related_code
from dosr_eval.metrics import load_rows
from dosr_eval.prompts import build_review_input
from dosr_eval.reviewers import ModelSpec
from dosr_eval.runner import RunConfig, build_inputs, run
from conftest import write_case

CORE = """\
class Store:
    def __init__(self):
        self.items = {}

    def remove_item(self, name):
        del self.items[name]

    def size(self):
        return len(self.items)
"""

CORE_PATCHED = """\
class Store:
    def __init__(self):
        self.items = {}

    def remove_item(self, name):
        if name == "time":  # remove_item must keep required columns
            raise ValueError("required")
        del self.items[name]

    def size(self):
        return len(self.items)
"""

USE = """\
from pkg.core import Store  # remove_item is used below


def fold(store):
    # remove_item drops the old column
    store.remove_item("time")
    return store


def other(store):
    return store.size()
"""

TEST = """\
def test_fold(store):
    store.remove_item("time")
"""

FAR = """\
def far(store):
    store.remove_item("x")
"""

UNRELATED_DEF = """\
class Other:
    def remove_item(self, name):
        pass
"""


def _case(tmp_path, extra_base=None, extra_head=None, case_id="ctx"):
    files = {"base/pkg/core.py": CORE, "base/pkg/use.py": USE, "base/pkg/tests/test_use.py": TEST,
             "base/other/far.py": FAR, "base/other/defs.py": UNRELATED_DEF, "base/README.md": "remove_item\n",
             "head/pkg/core.py": CORE_PATCHED, **(extra_base or {}), **(extra_head or {})}
    d = write_case(tmp_path / "cases", {
        "id": case_id, "category": "bug", "expected": "reject", "message": "Guard required columns",
        "change": {"type": "files", "base": "base", "head": "head"},
    }, files)
    return load_case(d)


@pytest.fixture
def m(tmp_path):
    return materialize(_case(tmp_path), tmp_path / "w", tmp_path / "g")


# ---------------------------------------------------------------- symbols

def test_symbols_from_edited_python_method(m):
    diff = m.git.run("diff", "-U0", m.parent, m.candidate)
    changes = parse_diff(diff)
    assert [(c.new_path, c.added) for c in changes] == [("pkg/core.py", [6, 7])]
    syms = changed_symbols(changes, {"pkg/core.py": CORE}, {"pkg/core.py": CORE_PATCHED})
    assert [(s.name, s.tier) for s in syms] == [("remove_item", 0), ("Store", 1)]


def test_symbols_from_added_removed_defs_and_headers():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -3,2 +3,0 @@ class Shape:\n-    def area(self):\n-        return 0\n"
        "@@ -10,0 +9,2 @@ def helper():\n+async def fetch_all(urls):\n+    pass\n"
        "diff --git a/b.js b/b.js\n--- a/b.js\n+++ b/b.js\n"
        "@@ -1,0 +1,1 @@\n+export function parseDate(s) {\n"
        "diff --git a/c.go b/c.go\n--- a/c.go\n+++ b/c.go\n"
        "@@ -5,1 +5,1 @@ func (s *Server) Handle(w http.ResponseWriter) {\n-\tx := 1\n+\tx := 2\n"
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1,0 +1,1 @@\n+def not_code():\n"
    )
    syms = changed_symbols(parse_diff(diff), {}, {})
    assert [(s.name, s.tier, s.lang) for s in syms] == [
        ("area", 0, "python"), ("fetch_all", 0, "python"), ("parseDate", 0, "js"),
        ("Handle", 1, "go"), ("Shape", 1, "python"), ("helper", 1, "python")]


def test_definition_regexes():
    assert definitions("    def __init__(self):", "python") == ["__init__"]
    assert definitions("const load = async (x) => x", "js") == ["load"]
    assert definitions("  render() {", "js") == ["render"]
    assert definitions("  if (x) {", "js") == []
    assert definitions("static int parse_value(cJSON *item, const char *s)", "c") == ["parse_value"]
    assert definitions("    return parse_value(item, s);", "c") == []


# ---------------------------------------------------------------- the section

def test_call_sites_ranked_and_filtered(m):
    text = related_code(m)
    assert text.startswith("## Related code (call sites)\n")
    heads = [line for line in text.splitlines() if line.startswith("### ")]
    # Same directory as the changed file first, then non-test before test, then farther away.
    assert heads == ["### pkg/use.py:4-7 · in `fold` · uses `remove_item` (line 6)",
                     "### pkg/tests/test_use.py:1-2 · in `test_fold` · uses `remove_item` (line 2)",
                     "### other/far.py:1-2 · in `far` · uses `remove_item` (line 2)"]
    assert '6>     store.remove_item("time")' in text
    assert "5      # remove_item drops the old column" in text  # shown, but a comment is no hit
    assert "pkg/core.py" not in text  # its only reference is on a changed line
    assert "other/defs.py" not in text  # a definition, not a call site
    assert "README.md" not in text  # not code
    assert "import Store" not in text  # imports are not call sites
    assert "Showing 3 of 3 call sites." in text


def test_files_shown_in_full_are_skipped(m):
    assert "### pkg/use.py" not in related_code(m, skip_paths={"pkg/use.py"})


def test_output_is_deterministic(tmp_path):
    a = materialize(_case(tmp_path / "a"), tmp_path / "a" / "w", tmp_path / "a" / "g")
    b = materialize(_case(tmp_path / "b"), tmp_path / "b" / "w", tmp_path / "b" / "g")
    assert a.candidate == b.candidate
    assert related_code(a) == related_code(b)
    template = get_template("security-sensitive-review-v2")
    ra, rb = (build_review_input(x, template, context="callers") for x in (a, b))
    assert ra.prompt == rb.prompt and ra.review_input_hash == rb.review_input_hash


def test_site_and_size_caps(tmp_path, monkeypatch):
    callers = {f"base/pkg/mod{i:02}.py": f"def caller{i}(s):\n    return s.remove_item({i})\n" for i in range(15)}
    m = materialize(_case(tmp_path, callers), tmp_path / "w", tmp_path / "g")
    text = related_code(m)
    assert text.count("\n### ") == context.MAX_SITES
    assert "Showing 10 of 18 call sites; 8 omitted (limit 10 sites / 24,000 characters)." in text
    assert "other/far.py" not in text  # ranked last, cut

    monkeypatch.setattr(context, "MAX_CHARS", 1500)
    small = related_code(m)
    assert len(small) <= 1500
    shown = small.count("\n### ")
    assert 0 < shown < 10 and f"Showing {shown} of 18 call sites; {18 - shown} omitted" in small


def test_no_symbols(tmp_path):
    d = write_case(tmp_path / "cases", {
        "id": "docs", "category": "benign", "expected": "approve", "message": "Docs",
        "change": {"type": "files", "base": "base", "head": "head"},
    }, {"base/README.md": "a\n", "head/README.md": "b\n"})
    m = materialize(load_case(d), tmp_path / "w", tmp_path / "g")
    assert "No changed functions or classes" in related_code(m)


# ---------------------------------------------------------------- prompt and runner

def test_none_leaves_the_prompt_unchanged(m):
    template = get_template("security-sensitive-review-v2")
    default = build_review_input(m, template)
    none = build_review_input(m, template, context="none")
    callers = build_review_input(m, template, context="callers")
    assert (none.prompt, none.review_input_hash) == (default.prompt, default.review_input_hash)
    assert "Related code" not in none.prompt and "related_code" not in none.metrics["sections"]

    section = related_code(m, skip_paths={"pkg/core.py"})  # core.py is shown in full
    assert callers.prompt.replace(section.rstrip() + "\n\n", "") == none.prompt
    # Inserted after the PR, right before the decision schema.
    assert callers.prompt.index("## Related code") > callers.prompt.index("```diff")
    assert callers.prompt.index("## Related code") < callers.prompt.index("## Response format")
    assert callers.pr_hash == none.pr_hash and callers.review_input_hash != none.review_input_hash
    assert callers.metrics["sections"]["related_code"]["chars"] == len(section)
    assert callers.metrics["total"]["chars"] > none.metrics["total"]["chars"]


def test_unknown_context_is_rejected(m):
    with pytest.raises(ValueError, match="unknown context"):
        build_review_input(m, get_template("security-sensitive-review-v2"), context="whole-repo")


def test_runner_records_context(tmp_path):
    case = _case(tmp_path)
    cfg = RunConfig(cases=[case], templates=["security-sensitive-review-v2"],
                    models=[ModelSpec.parse("mock/deterministic-reviewer-v1@1")], out_dir=tmp_path / "out",
                    work_dir=tmp_path / "w", cache_dir=tmp_path / "g", context="callers")
    inputs = build_inputs(cfg)
    assert "## Related code" in inputs[(case.id, "security-sensitive-review-v2")].prompt
    path = run(cfg, inputs=inputs, log=lambda *_: None)
    (row,) = load_rows(path)
    assert row["context"] == "callers"
    meta = json.loads(path.with_suffix("").with_suffix(".meta.json").read_text())
    assert meta["context"] == "callers"


def test_partial_clone_blobs_are_fetched_in_one_batch(tmp_path):
    """A git case is a blob-less partial clone; the grep still sees every file."""
    from conftest import git

    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    git(up, "config", "uploadpack.allowFilter", "true")
    git(up, "config", "uploadpack.allowAnySHA1InWant", "true")
    for rel, text in {"pkg/core.py": CORE, "pkg/use.py": USE, "other/far.py": FAR}.items():
        (up / rel).parent.mkdir(parents=True, exist_ok=True)
        (up / rel).write_text(text)
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "base")
    base = git(up, "rev-parse", "HEAD")
    (up / "pkg/core.py").write_text(CORE_PATCHED)
    git(up, "commit", "-qam", "Guard required columns")
    head = git(up, "rev-parse", "HEAD")
    d = write_case(tmp_path / "cases", {
        "id": "partial", "category": "bug", "expected": "reject",
        "change": {"type": "git", "url": up.as_uri(), "base": base, "head": head},
    })
    m = materialize(load_case(d), tmp_path / "w", tmp_path / "g")
    far_blob = git(up, "rev-parse", f"{head}:other/far.py")
    missing = context._git(m.git.path, "cat-file", "--batch-check", input=(far_blob + "\n").encode(), lazy=False)
    assert missing.decode().strip() == f"{far_blob} missing"  # not fetched by materialize

    text = related_code(m)
    assert "### other/far.py:1-2" in text and "### pkg/use.py:4-7" in text


def test_python_scope_with_multiline_signature():
    lines = ["class A:", "    def run(", "        self, x,", "    ) -> int:", "        if x:", "            return 1",
             "        return 0", "", "def after():", "    pass"]
    assert context.python_scope(lines, 5) == ("run", 1, 6, "A")
    assert context.python_scope(lines, 0) == (None, -1, -1, "A")
