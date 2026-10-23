# todo-js style guide

Included in every review prompt via the policy's `review.context_files`.

- CommonJS modules, no runtime dependencies (`package.json` stays dependency-free).
- Methods that change the list return `this`, so calls can be chained.
- Every new method gets a test under `test/` using `node:test`.
