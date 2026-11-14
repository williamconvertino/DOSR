# dosr-chain (`dosr_chain`)

Client-side chain adapters, with the same methods and return shapes so the client
pipeline, the client-side validator and the attestor work with either:

| Adapter | What it is | Shared? |
|---|---|---|
| `EvmChain` (`evm.py`) | the [DOSRRegistry contract](../../contracts/README.md) over JSON-RPC (httpx + eth-abi; transactions signed locally with eth-account) | yes: everyone pointed at the RPC URL + contract sees one canonical HEAD |
| `MockChain` (`mock_chain.py`) | a JSON file (`$DOSR_CHAIN_STATE` or `~/.dosr/mock-chain/state.json`) enforcing the same checks | no: one per device; tests and offline use |

`connect()` picks one: `DOSR_CHAIN` (`evm`/`mock`; default `evm` when `DOSR_RPC_URL` is
set), `DOSR_RPC_URL` (default `http://127.0.0.1:8545`), `DOSR_CONTRACT` (default: the
dev deployment address), `DOSR_CHAIN_PRIVATE_KEY` (sender), `DOSR_CONFIRMATIONS`.

Common API: `register_repository`, `get_repository`, `get_head`, `list_repositories`,
`accept_transition` (raises `StaleParentError` with the new `head` when another
transition landed on the same parent first), `publish_snapshot`, `describe`.
EVM extras: `transitions`, `signed_transitions` (every signed attestation rebuilt from
the transactions' calldata), `canonical_index`, `snapshots`, `activity` (decoded recent
transactions, including revert reasons), `send_transition` / `transition_result` /
`set_automine` / `mine` (race demos), `as_account(key)` (same connection, another sender),
and `deploy` / `ensure_deployed`.

`dev.py`: the dev chain's prefunded test accounts (anvil's public mnemonic: **dev only**;
account 1 is the dev attestor key, 2–5 are the demo profiles' `chain_account`s) and
`Anvil`, a context manager that runs a throwaway anvil node for tests.
