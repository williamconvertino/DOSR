"""Optional extra review context: code elsewhere in the repository that uses what a PR changes.

`--context callers` appends a "## Related code (call sites)" section to the PR Markdown.
This is a harness setting for experiments, not part of the protocol: the attestor does
not build it. To be adoptable later it must still be a pure function of the transition
(same parent/candidate -> same text), since the attestor signs the prompt's hash. So
everything here reads only Git objects of the two commits and orders results by fixed
keys, never by timestamps, file-system order or network state.

How the section is built:

1. Changed symbols: names of functions/classes the diff defines, removes or edits
   (added/removed `def`/`class` lines, the hunk header's function context, and for
   Python the innermost `def` around each changed line, found by indentation).
   JS/TS, Go and C get a simple regex fallback for definition lines.
2. `git grep -w` for those names over the candidate tree (files of the same language),
   skipping the changed lines, definitions, imports, comments, and files the prompt
   already shows in full.
3. Each hit becomes a snippet: its enclosing Python function when short, else
   +/-15 lines; overlapping snippets in one file are merged into one site.
4. Sites are ranked: same directory as a changed file, then closer packages, non-test
   before test files, direct symbols before enclosing classes, then path and line.
   At most MAX_SITES sites and MAX_CHARS characters; the section says how many were left out.

Whole-file context tends to hurt reviewers (SWE-PRBench), hence capped snippets.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .cases import Materialized

CONTEXTS = ("none", "callers")
HEADING = "## Related code (call sites)"

MAX_SITES = 10
MAX_CHARS = 24_000  # whole section, about 6k tokens by the chars/4 rule
WINDOW = 15  # lines around a hit when the enclosing function is unknown or too long
MAX_FUNCTION_LINES = 60  # show the whole enclosing function up to this size
MAX_SNIPPET_LINES = 60  # merged windows stop growing here
MAX_LINE_CHARS = 200
MAX_SYMBOLS = 12
MAX_HITS = 500  # references examined; any beyond are only counted
MAX_FILE_BYTES = 1_000_000

LANGS = {
    "python": (".py", ".pyi", ".pyx"),
    "js": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    "go": (".go",),
    "c": (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"),
}
FENCE_LANG = {"python": "python", "js": "javascript", "go": "go", "c": "c"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "sizeof", "function", "else", "do", "with",
            "elif", "not", "and", "or", "new", "typeof", "await", "defined"}

_NAME = r"[A-Za-z_]\w*"
_DEF = {
    "python": [re.compile(rf"^\s*(?:async\s+)?def\s+({_NAME})"), re.compile(rf"^\s*class\s+({_NAME})")],
    "js": [
        re.compile(rf"\bfunction\s*\*?\s*({_NAME})\s*\("),
        re.compile(rf"\bclass\s+({_NAME})"),
        re.compile(rf"\b(?:const|let|var)\s+({_NAME})\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|{_NAME}\s*=>)"),
        re.compile(rf"^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*({_NAME})\s*\([^)]*\)\s*\{{"),
    ],
    "go": [re.compile(rf"^func\s+(?:\([^)]*\)\s*)?({_NAME})\s*[\[(]"), re.compile(rf"^type\s+({_NAME})\s")],
    "c": [re.compile(rf"^(?:{_NAME}[\s*]+)+?\**({_NAME})\s*\([^;]*$")],
}
_COMMENT = {"python": ("#",), "js": ("//", "/*", "*"), "go": ("//", "/*", "*"), "c": ("//", "/*", "*")}
_IMPORT = {
    "python": re.compile(r"^\s*(?:import|from)\s"),
    "js": re.compile(r"^\s*import\s|\brequire\s*\("),
    "go": re.compile(r"^\s*import\s"),
    "c": re.compile(r"^\s*#\s*include\b"),
}
_PY_DEF_LINE = re.compile(rf"^(\s*)(?:async\s+)?def\s+({_NAME})")
_PY_CLASS_LINE = re.compile(rf"^(\s*)class\s+({_NAME})")


class ContextError(RuntimeError):
    pass


def language(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    return next((lang for lang, exts in LANGS.items() if suffix in exts), None)


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    return (any(p.lower() in ("test", "tests", "testing", "__tests__", "spec", "specs") for p in parts[:-1])
            or name.startswith("test_") or re.search(r"(_test|\.test|\.spec|_spec)\.\w+$", name) is not None
            or name in ("conftest.py",))


# ---------------------------------------------------------------- git

def _git(repo, *args: str, input: bytes | None = None, lazy: bool = True) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    if not lazy:
        env["GIT_NO_LAZY_FETCH"] = "1"
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=repo, input=input,
                          capture_output=True, env=env)
    if proc.returncode != 0:
        raise ContextError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _read_blobs(repo, names: list[str]) -> dict[str, str | None]:
    """`rev:path` -> text (None if missing, binary or too large), in one `cat-file --batch`."""
    if not names:
        return {}
    out = _git(repo, "cat-file", "--batch", input="".join(n + "\n" for n in names).encode())
    texts, pos = {}, 0
    for name in names:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(header) < 3 or header[1] != "blob":
            texts[name] = None  # "<name> missing" (or not a blob): no body follows
            continue
        size = int(header[2])
        data = out[pos:pos + size]
        pos += size + 1
        texts[name] = None if size > MAX_FILE_BYTES or b"\0" in data[:8000] else data.decode("utf-8", "replace")
    return texts


def _promisor_remote(repo) -> str | None:
    proc = subprocess.run(["git", "config", "--get-regexp", r"^remote\..*\.promisor$"], cwd=repo,
                          capture_output=True, text=True)
    for line in sorted(proc.stdout.splitlines()):
        key, _, value = line.partition(" ")
        if value.strip() == "true":
            return key[len("remote."):-len(".promisor")]
    return None


def _prefetch(repo, candidate: str, langs: set[str]) -> None:
    """In a partial clone, fetch the blobs `git grep` will read in one batch, instead of
    letting git fetch them lazily one by one. Changes only what is cached, not the output."""
    remote = _promisor_remote(repo)
    if remote is None:
        return
    exts = tuple(e for lang in langs for e in LANGS[lang])
    oids = []
    for entry in _git(repo, "ls-tree", "-r", "-z", candidate).split(b"\0"):
        if entry:
            meta, path = entry.decode("utf-8", "replace").split("\t", 1)
            _, kind, oid = meta.split()
            if kind == "blob" and path.lower().endswith(exts):
                oids.append(oid)
    if not oids:
        return
    check = _git(repo, "cat-file", "--batch-check", input="".join(o + "\n" for o in oids).encode(), lazy=False)
    missing = [line.split()[0] for line in check.decode().splitlines() if line.endswith(" missing")]
    if missing:
        _git(repo, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no",
             "--filter=blob:none", "--stdin", remote, input="".join(o + "\n" for o in missing).encode())


# ---------------------------------------------------------------- changed symbols

@dataclass
class FileChange:
    old_path: str | None
    new_path: str | None
    added: list[int] = field(default_factory=list)  # line numbers in the candidate
    removed: list[int] = field(default_factory=list)  # line numbers in the parent
    added_text: list[str] = field(default_factory=list)
    removed_text: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)  # hunk header function context


def _strip_prefix(raw: str, prefix: str) -> str | None:
    raw = raw.split("\t")[0]
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].encode().decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return raw[len(prefix):] if raw.startswith(prefix) else raw


def parse_diff(diff: str) -> list[FileChange]:
    """Changed line numbers and texts per file, from a unified diff (best with -U0)."""
    files: list[FileChange] = []
    cur, in_hunk, old_no, new_no = None, False, 0, 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            cur, in_hunk = FileChange(None, None), False
            files.append(cur)
        elif cur is None:
            continue
        elif not in_hunk and line.startswith("--- "):
            cur.old_path = _strip_prefix(line[4:], "a/")
        elif not in_hunk and line.startswith("+++ "):
            cur.new_path = _strip_prefix(line[4:], "b/")
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)", line)
            if m:
                in_hunk, old_no, new_no = True, int(m.group(1)), int(m.group(2))
                if m.group(3).strip():
                    cur.headers.append(m.group(3))
        elif in_hunk and line.startswith("+"):
            cur.added.append(new_no)
            cur.added_text.append(line[1:])
            new_no += 1
        elif in_hunk and line.startswith("-"):
            cur.removed.append(old_no)
            cur.removed_text.append(line[1:])
            old_no += 1
        elif in_hunk and line.startswith(" "):
            old_no, new_no = old_no + 1, new_no + 1
    return files


def definitions(line: str, lang: str) -> list[str]:
    """Names a source line defines (function, method or class), by regex."""
    names = []
    for rx in _DEF.get(lang, ()):
        m = rx.search(line)
        if m and m.group(1) not in KEYWORDS:
            names.append(m.group(1))
    return names


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def python_scope(lines: list[str], index: int) -> tuple[str | None, int, int, str | None]:
    """Innermost `def` around lines[index] (0-based) by indentation:
    (name, first line, last line, enclosing class name); (None, -1, -1, class) if none."""
    func, start, cls = None, -1, None
    threshold = _indent(lines[index]) if lines[index].strip() else 10**9
    if _PY_DEF_LINE.match(lines[index]) or _PY_CLASS_LINE.match(lines[index]):
        threshold += 1  # a def line belongs to its own scope
    for j in range(index, -1, -1):
        text = lines[j]
        if not text.strip() or text.lstrip().startswith((")", "]", "}")):
            continue  # blank, or the end of a bracketed continuation (e.g. a multi-line signature)
        ind = _indent(text)
        if ind >= threshold:
            continue
        threshold = ind
        d, c = _PY_DEF_LINE.match(text), _PY_CLASS_LINE.match(text)
        if d and func is None:
            func, start = d.group(2), j
        elif c:
            cls = c.group(2)
            break
        if ind == 0:
            break
    if func is None:
        return None, -1, -1, cls
    end, base = start, _indent(lines[start])
    for k in range(start + 1, len(lines)):
        if lines[k].strip():
            if _indent(lines[k]) <= base and not lines[k].lstrip().startswith((")", "]", "}")):
                break
            end = k
    return func, start, end, cls


@dataclass(frozen=True)
class Symbol:
    name: str
    tier: int  # 0: defined, removed or edited function; 1: enclosing class / hunk context
    lang: str


def changed_symbols(changes: list[FileChange], old_text: dict, new_text: dict) -> list[Symbol]:
    """Functions and classes the diff touches, direct ones (tier 0) first, then by name."""
    best: dict[str, Symbol] = {}

    def add(name: str, tier: int, lang: str) -> None:
        if len(name) < 3 or (name.startswith("__") and name.endswith("__")) or name in KEYWORDS:
            return
        if name not in best or tier < best[name].tier:
            best[name] = Symbol(name, tier, lang)

    for fc in changes:
        lang = language(fc.new_path or fc.old_path or "")
        if lang is None:
            continue
        for text in fc.added_text + fc.removed_text:
            for name in definitions(text, lang):
                add(name, 0, lang)
        for header in fc.headers:
            for name in definitions(header, lang):
                add(name, 1, lang)
        if lang == "python":
            for numbers, text in ((fc.added, new_text.get(fc.new_path)), (fc.removed, old_text.get(fc.old_path))):
                lines = text.split("\n") if text else []
                for n in numbers:
                    if 0 < n <= len(lines):
                        func, _, _, cls = python_scope(lines, n - 1)
                        if func and not (func.startswith("__") and func.endswith("__")):
                            add(func, 0, lang)
                        if cls:
                            add(cls, 1, lang)
    return sorted(best.values(), key=lambda s: (s.tier, s.name))[:MAX_SYMBOLS]


# ---------------------------------------------------------------- call sites

@dataclass
class Hit:
    path: str
    line: int  # 1-based
    symbols: tuple[str, ...]
    tier: int


@dataclass
class Site:
    path: str
    start: int  # 0-based, inclusive
    end: int
    hits: list[Hit]
    scope: str | None = None


def _proximity(path: str, changed_dirs: list[PurePosixPath]) -> tuple[int, int]:
    d = PurePosixPath(path).parent
    if d in changed_dirs:
        return 0, 0
    common = max((len(_common(d, c)) for c in changed_dirs), default=0)
    return 1, -common


def _common(a: PurePosixPath, b: PurePosixPath) -> tuple:
    out = []
    for x, y in zip(a.parts, b.parts):
        if x != y:
            break
        out.append(x)
    return tuple(out)


def _grep(repo, candidate: str, symbols: list[Symbol]) -> list[tuple[str, int, str]]:
    langs = sorted({s.lang for s in symbols})
    pathspecs = [f"*{e}" for lang in langs for e in LANGS[lang]] + [":(exclude)*.min.js"]
    args = ["grep", "-z", "-n", "-w", "-F", "-I", "--full-name", "--no-color"]
    for s in symbols:
        args += ["-e", s.name]
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args, candidate, "--", *pathspecs], cwd=repo,
                          capture_output=True,
                          # _prefetch got every blob; never fall back to fetching them one by one.
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"})
    if proc.returncode not in (0, 1):  # 1: no match
        raise ContextError(f"git grep failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    out = []
    prefix = candidate + ":"
    for record in proc.stdout.decode("utf-8", "replace").split("\n"):
        parts = record.split("\0", 2)
        if len(parts) == 3 and parts[0].startswith(prefix):
            out.append((parts[0][len(prefix):], int(parts[1]), parts[2]))
    return out


def find_hits(raw: list[tuple[str, int, str]], symbols: list[Symbol], changed_lines: dict[str, set[int]],
              skip_paths: set[str]) -> list[Hit]:
    by_name = {s.name: s for s in symbols}
    words = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(by_name)) + r")\b")
    hits = []
    for path, line, text in raw:
        lang = language(path)
        if lang is None or path in skip_paths or line in changed_lines.get(path, ()):
            continue
        stripped = text.strip()
        if stripped.startswith(_COMMENT[lang]) or _IMPORT[lang].search(text):
            continue
        defined = set(definitions(text, lang))
        names = sorted({n for n in words.findall(text) if n in by_name and n not in defined})
        if names:
            hits.append(Hit(path, line, tuple(names), min(by_name[n].tier for n in names)))
    return hits


def _snippet_range(lines: list[str], hit: Hit, lang: str) -> tuple[int, int, str | None]:
    i = hit.line - 1
    if lang == "python":
        func, start, end, _ = python_scope(lines, i)
        if func and end - start + 1 <= MAX_FUNCTION_LINES:
            return start, end, func
        lo, hi = max(i - WINDOW, start if func else 0), min(i + WINDOW, end if func else len(lines) - 1)
        return lo, hi, func
    return max(i - WINDOW, 0), min(i + WINDOW, len(lines) - 1), None


def build_sites(hits: list[Hit], texts: dict[str, str | None]) -> list[Site]:
    """One site per snippet; overlapping snippets in a file merge (up to MAX_SNIPPET_LINES)."""
    sites: list[Site] = []
    for path in sorted({h.path for h in hits}):
        text = texts.get(path)
        if text is None:
            continue
        lines = text.split("\n")
        open_site = None
        for h in sorted((h for h in hits if h.path == path), key=lambda h: h.line):
            if h.line > len(lines):
                continue
            start, end, scope = _snippet_range(lines, h, language(path))
            if (open_site and start <= open_site.end + 1
                    and max(end, open_site.end) - open_site.start + 1 <= MAX_SNIPPET_LINES):
                open_site.end = max(end, open_site.end)
                open_site.hits.append(h)
                if open_site.scope != scope:
                    open_site.scope = None
            else:
                open_site = Site(path, start, end, [h], scope)
                sites.append(open_site)
    return sites


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_site(site: Site, lines: list[str]) -> str:
    hit_lines = {h.line for h in site.hits}
    start, end = site.start, site.end
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end].strip():
        end -= 1
    width = len(str(end + 1))
    body = []
    for n in range(start, end + 1):
        text = lines[n]
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " …"
        body.append(f"{n + 1:>{width}}{'>' if n + 1 in hit_lines else ' '} {text}".rstrip())
    symbols = sorted({s for h in site.hits for s in h.symbols})
    refs = ", ".join(str(h.line) for h in site.hits)
    head = (f"### {site.path}:{start + 1}-{end + 1}"
            + (f" · in `{site.scope}`" if site.scope else "")
            + f" · uses {', '.join(f'`{s}`' for s in symbols)} (line{'s' if len(site.hits) > 1 else ''} {refs})")
    code = "\n".join(body)
    fence = _fence(code)
    return "\n".join([head, "", fence + FENCE_LANG.get(language(site.path), ""), code, fence, ""])


def _intro(symbols: list[Symbol], shown: int, total: int, unexamined: int) -> str:
    names = ", ".join(f"`{s.name}`" for s in symbols)
    omitted = total - shown
    lines = [
        HEADING,
        "",
        f"Code elsewhere in the repository at the candidate commit that references names this change "
        f"defines or modifies ({names}). Found by name search, so some may be unrelated; `>` marks the "
        f"referencing lines.",
        "",
        f"Showing {shown} of {total} call site{'s' if total != 1 else ''}"
        + (f"; {omitted} omitted (limit {MAX_SITES} sites / {MAX_CHARS:,} characters)" if omitted else "")
        + (f"; {unexamined} further reference{'s' if unexamined != 1 else ''} not examined" if unexamined else "")
        + ".",
        "",
    ]
    return "\n".join(lines)


def related_code(m: Materialized, skip_paths: set[str] = frozenset()) -> str:
    """The "## Related code (call sites)" Markdown section for a transition.
    `skip_paths`: files the prompt already shows in full (their hits add nothing)."""
    repo, parent, candidate = m.git.path, m.parent, m.candidate
    diff = _git(repo, "diff", "--no-color", "--no-ext-diff", "-M", "-U0", parent, candidate).decode("utf-8", "replace")
    changes = parse_diff(diff)
    py = [fc for fc in changes if language(fc.new_path or fc.old_path or "") == "python"]
    names = ([f"{parent}:{fc.old_path}" for fc in py if fc.old_path]
             + [f"{candidate}:{fc.new_path}" for fc in py if fc.new_path])
    blobs = _read_blobs(repo, names)
    old_text = {fc.old_path: blobs.get(f"{parent}:{fc.old_path}") for fc in py if fc.old_path}
    new_text = {fc.new_path: blobs.get(f"{candidate}:{fc.new_path}") for fc in py if fc.new_path}
    symbols = changed_symbols(changes, old_text, new_text)
    if not symbols:
        return f"{HEADING}\n\nNo changed functions or classes found in the diff, so no call sites to show.\n"

    _prefetch(repo, candidate, {s.lang for s in symbols})
    changed_lines = {fc.new_path: set(fc.added) for fc in changes if fc.new_path}
    changed_dirs = sorted({PurePosixPath(p).parent for fc in changes for p in (fc.new_path, fc.old_path) if p})
    hits = find_hits(_grep(repo, candidate, symbols), symbols, changed_lines, set(skip_paths))

    def rank(h: Hit) -> tuple:
        return (*_proximity(h.path, changed_dirs), is_test_path(h.path), h.tier, h.path, h.line)

    hits.sort(key=rank)
    unexamined = max(len(hits) - MAX_HITS, 0)
    hits = hits[:MAX_HITS]
    files = sorted({h.path for h in hits})
    blobs = _read_blobs(repo, [f"{candidate}:{p}" for p in files])
    texts = {p: blobs[f"{candidate}:{p}"] for p in files}
    sites = sorted(build_sites(hits, texts), key=lambda s: rank(s.hits[0]))

    if not sites:
        return (f"{HEADING}\n\nNo references found outside the changed lines to the names this change "
                f"defines or modifies ({', '.join(f'`{s.name}`' for s in symbols)}).\n")
    # Reserve room for the longest intro (with the "omitted" clause).
    budget = MAX_CHARS - len(_intro(symbols, 0, len(sites), unexamined)) - 16
    blocks, used = [], 0
    for site in sites:
        if len(blocks) == MAX_SITES:
            break
        block = render_site(site, texts[site.path].split("\n"))
        if used + len(block) + 1 <= budget:
            blocks.append(block)
            used += len(block) + 1
    return _intro(symbols, len(blocks), len(sites), unexamined) + "\n" + "\n".join(blocks)


def context_section(m: Materialized, context: str, skip_paths: set[str] = frozenset()) -> str:
    """Extra Markdown for a context mode ("" for "none")."""
    if context == "none":
        return ""
    if context == "callers":
        return related_code(m, skip_paths)
    raise ValueError(f"unknown context {context!r}: expected one of {', '.join(CONTEXTS)}")
