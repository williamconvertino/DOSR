# Contributing to calculator-py

This file is listed in the repository policy (`review.context_files`), so every PR
sent for review includes it as context. Reviewers check changes against these rules:

1. Every public function has a one-line docstring and a test in `tests/`.
2. Operations are pure: no I/O, no global state, no `eval`/`exec`, no imports beyond
   the standard library.
3. Errors are raised, not returned (e.g. `divide` raises `ZeroDivisionError`; it never
   returns a sentinel such as 0 or `None`).
4. Existing behavior only changes when the commit message says so.
