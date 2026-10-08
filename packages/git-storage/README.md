# dosr-git-storage (`dosr_git`)

Git adapter used by the client: a thin wrapper over the `git` CLI (`GitRepo`) and
bundle creation for a `parent -> candidate` transition (`create_bundle`).

- `full` bundles contain everything reachable from the candidate (self-contained; v0.1 default).
- `thin` bundles contain only `parent..candidate` (receiver must already have the parent).

TODO: IPFS publish/fetch adapter; verifying fetched bundles against the canonical OID.
