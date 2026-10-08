# dosr-chain (`dosr_chain`)

Client-side chain adapter. **v0.1 ships only `MockChain`**, a JSON-file stand-in for
the repository contract (state path: `$DOSR_CHAIN_STATE` or `~/.dosr/mock-chain/state.json`).

It enforces the contract's deterministic checks (registered repo, genesis policy hash,
attestor in policy, approved, not expired, chain/contract domain, `parent == HEAD`)
but does **not** verify signatures (the attestor's signature is still a placeholder).

Method names mirror the planned contract (`register_repository`, `get_head`,
`accept_transition`) so an RPC-backed adapter can replace it without pipeline changes.
