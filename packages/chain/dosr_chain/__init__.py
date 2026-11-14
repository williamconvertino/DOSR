"""DOSR chain adapters.

- `EvmChain`: the DOSRRegistry contract over JSON-RPC (a local anvil dev chain in the demo).
  One chain shared by every client and attestor that points at it.
- `MockChain`: a JSON file per device that enforces the same checks; for tests and for
  running without a node.

`connect()` picks one from arguments or the environment:

    DOSR_CHAIN              evm | mock (default: evm if an RPC URL is given, else mock)
    DOSR_RPC_URL            JSON-RPC endpoint (default http://127.0.0.1:8545)
    DOSR_CONTRACT           contract address (default: the dev deployment address)
    DOSR_CHAIN_PRIVATE_KEY  sender of transactions (default: dev account 0)
    DOSR_CONFIRMATIONS      read state this many blocks behind the tip (default 0)
    DOSR_CHAIN_STATE        mock chain state file
"""

import os

from .errors import ChainError, StaleParentError
from .mock_chain import MockChain, default_state_path


def connect(kind: str | None = None, *, rpc_url: str | None = None, contract: str | None = None,
            private_key: str | None = None, state_path=None, confirmations: int | None = None):
    env = os.environ
    rpc_url = rpc_url or env.get("DOSR_RPC_URL")
    kind = (kind or env.get("DOSR_CHAIN") or ("evm" if rpc_url else "mock")).lower()
    if kind == "mock":
        return MockChain(state_path or env.get("DOSR_CHAIN_STATE") or None)
    if kind != "evm":
        raise ChainError(f"unknown chain kind {kind!r} (evm or mock)")
    from .evm import EvmChain

    return EvmChain(rpc_url, contract or env.get("DOSR_CONTRACT"),
                    private_key or env.get("DOSR_CHAIN_PRIVATE_KEY"),
                    confirmations if confirmations is not None else int(env.get("DOSR_CONFIRMATIONS", "0") or 0))


def __getattr__(name):  # lazy: importing the mock should not pull in httpx / eth-abi
    if name in ("EvmChain", "deploy", "ensure_deployed"):
        from . import evm

        return getattr(evm, name)
    raise AttributeError(name)


__all__ = ["ChainError", "StaleParentError", "MockChain", "EvmChain", "connect", "default_state_path",
           "deploy", "ensure_deployed"]
