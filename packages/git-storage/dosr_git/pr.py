"""Collects what a PR template needs from Git.

Shared by the client (to build the PR it shows the user) and the attestor (to
rebuild the PR from the bundle instead of trusting the client), so both produce
the same PR and the same `pr_hash`.
"""

from dosr_protocol.pr_format import PRTemplate, select_context_files

from .repo import GitRepo


def pr_inputs(git: GitRepo, template: PRTemplate, parent: str, candidate: str) -> dict:
    """Keyword arguments for `dosr_protocol.pr_format.build_pr` (minus template/repo/OIDs)."""
    files = git.changed_files(parent, candidate)
    full_files = {}
    if template.include_full_files:
        for f in files:
            if f["status"] != "D":
                text = git.show_file(candidate, f["path"], template.max_file_bytes)
                if text is not None:
                    full_files[f["path"]] = text
    context = {}
    for path in select_context_files(template, git.ls_tree(candidate), {f["path"] for f in files}):
        text = git.show_file(candidate, path, template.max_file_bytes)
        if text is not None:
            context[path] = text
    return {
        "commits": git.commits_between(parent, candidate),
        "files": files,
        "diff_text": git.diff(parent, candidate, template.diff_context_lines),
        "full_files": full_files,
        "context_files": context,
    }
