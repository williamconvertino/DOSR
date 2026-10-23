# PR templates

A PR template decides three things:
- what the client and the attestor collect from Git for a review: diff context, full files, and
  extra repository files;
- what the reviewer is told: the instructions and the checklist;
- how the PR is laid out as Markdown, which is the body of the review prompt.

Templates are TOML (or JSON) files. The three built-in templates are files too, in
`packages/protocol/dosr_protocol/templates/`. This folder holds the demo's custom template,
`team-review-v1.toml`, which the todo-js seed uses.

## Make one

```bash
dosr template new my-review-v1 --from security-sensitive-review-v2   # writes my-review-v1.toml
# edit it ...
dosr template check my-review-v1.toml --preview                      # validate + show the PR text
dosr init ../my-repo --template my-review-v1.toml                    # use it in a new repository
```

Use `dosr template list` to see the built-in templates and the template files that can be found.
`dosr template show [ID|FILE] [--toml] [--preview]` shows any template; with no argument it
shows the current repository's.

`--template` takes either a file path or a template id. An id is looked up in the directories
listed in `$DOSR_TEMPLATES` and in `~/.dosr/templates`. `demo.py` adds this folder to that list.

In the GUI, the *+ New* repository dialog has **View** and **+ New template…**. The editor
starts from any existing template and re-renders the preview on a sample change on every
*Check & preview*. *Save template* writes `<id>.toml` into the first templates folder.

## File format (`dosr-pr-template-v1`)

```toml
format = "dosr-pr-template-v1"
id = "my-review-v1"              # lowercase letters, digits, . _ -  (put a version in it)
description = "One line for pickers"

[context]
diff_context_lines = 3           # unified-diff context
include_full_files = true        # full text of each changed file
max_file_bytes = 64000           # bigger files are left out
files = ["README*", "docs/*.md"] # extra repository files (file-name or path globs)

[review]
instructions = "What the reviewer must check ..."
checklist = ["No secrets", "Tests updated"]

[[sections]]                     # layout, top to bottom; omit all [[sections]] for the default
type = "text"
body = "# {title}"
[[sections]]
type = "diff"
heading = "Patch"
```

| Section type | Options (defaults) |
|---|---|
| `text` | `body` (required), `heading` |
| `description` | `heading` ("Description"). Left out when no commit has a body. |
| `commits` | `heading` ("Commits"), `item` (``"- `{oid_short}` {subject} — {author}"``) |
| `changed_files` | `heading` ("Changed files"), `item` (``"- `{status}` {path} (+{additions}/-{deletions})"``) |
| `instructions` | `heading` ("Review instructions"), `item` (`"- [ ] {item}"`), `include_checklist` (true) |
| `diff` | `heading` ("Diff"), `fence` ("diff") |
| `full_files` / `context_files` | `heading`, `file_heading` (`"### {path}"`), `fence` (""). Left out when empty. |

`body`, `heading`, `item` and `file_heading` are format strings. `{name}` inserts a variable, and
`{{`/`}}` write literal braces.

**Available in every section:** `title`, `repo_name`, `repo_id`, `repo_id_short`, `parent`,
`candidate`, `template_id`, `template_hash`, `format`, `pr_hash`, `changed_files`, `additions`,
`deletions`, `patch_bytes`, `commit_count`.

**Available per item:**

| Section | Item variables |
|---|---|
| `commits` | `oid`, `oid_short`, `subject`, `author` |
| `changed_files` | `path`, `status`, `additions`, `deletions` |
| `instructions` | `item` |
| file sections | `path` |

When a template is loaded, any unknown key, section type or variable is an error that names the
problem. A typo fails at `check` time, not halfway through a review.

The decision-format instructions (the JSON the model must reply with) always come after the PR.
They belong to the policy's `decision_schema`, not to the template.

## How a template is bound to a repository

- **Built-in templates** are named by id in `review.prompt_template`.
- **Custom templates** are embedded in the policy as `review.template`, together with
  `review.prompt_template = <its id>`. The policy hash registered on chain at genesis covers the
  template, and the policy is sent with every review request. So the attestor always lays the PR
  out with exactly this template, with no shared registry, and the client blocks if the two
  disagree (`client_prompt_matches`).
- **The hash is recorded.** Every PR records `template_hash`, the sha256 of the template's
  canonical JSON.
- **Never change a template under the same id** once a repository uses it. Copy it to `-v2`.
  Editing a file never affects repositories that already embed it.

## Changing what templates can do

The format is versioned (`format`). To support new layout ideas:
1. Add a section type to `SECTION_TYPES` in `dosr_protocol/templates.py`.
2. Add its renderer to `_render_section` in `dosr_protocol/pr_format.py`.

The client and the attestor share this code, so they stay in sync. For an incompatible change,
add `dosr-pr-template-v2` and keep handling v1.
