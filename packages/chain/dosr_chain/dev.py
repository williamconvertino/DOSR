"""Local development chain accounts (anvil / Hardhat defaults).

DEV ONLY. These keys come from the public test mnemonic every anvil and Hardhat node
prefunds; anyone can sign with them. Never send real funds to these addresses.

    0  deployer of the DOSR contract (and the fallback sender)
    1  the dev attestor's signing key (services/attestor DEV_PRIVATE_KEY); it never sends txs
    2+ demo client profiles (`chain_account` in demo/clients/*.json)
"""

from functools import lru_cache

DEV_MNEMONIC = "test test test test test test test test test test test junk"
DEV_CHAIN_ID = 31337
DEPLOYER_INDEX = 0
DEFAULT_RPC_URL = "http://127.0.0.1:8545"
# Where account 0's first transaction on a fresh dev chain deploys the contract.
DEV_CONTRACT_ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


@lru_cache(maxsize=32)
def dev_account(index: int):
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(DEV_MNEMONIC, account_path=f"m/44'/60'/0'/0/{index}")


def dev_key(index: int) -> str:
    return "0x" + bytes(dev_account(index).key).hex()


def dev_address(index: int) -> str:
    return dev_account(index).address


def dev_address_book(count: int = 10) -> dict:
    """lowercase address -> dev account index (for labelling senders in the explorer)."""
    return {dev_address(i).lower(): i for i in range(count)}


# ---------------------------------------------------------------- tests: a throwaway anvil


def find_anvil():
    """anvil from $DOSR_ANVIL, PATH, or the demo's download (demo/.foundry/bin)."""
    import os
    import shutil
    import sys
    from pathlib import Path

    if os.environ.get("DOSR_ANVIL"):
        return Path(os.environ["DOSR_ANVIL"])
    found = shutil.which("anvil")
    if found:
        return Path(found)
    local = Path(__file__).resolve().parents[3] / "demo" / ".foundry" / "bin" / (
        "anvil.exe" if sys.platform == "win32" else "anvil")
    return local if local.exists() else None


class Anvil:
    """A fresh in-memory anvil node on a free port, with the DOSR contract deployed.

        with Anvil() as node:
            chain = node.chain(private_key=dev_key(2))
    """

    def __init__(self, binary=None):
        self.binary = binary or find_anvil()
        self.proc = None
        self.rpc_url = None
        self.contract = None

    def __enter__(self):
        import socket
        import subprocess
        import time

        from .evm import deploy, has_contract
        from .evm import Rpc

        if not self.binary:
            raise RuntimeError("anvil not found (set DOSR_ANVIL, or run `demo.py chain install`)")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.rpc_url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([str(self.binary), "--port", str(port), "--chain-id", str(DEV_CHAIN_ID),
                                      "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rpc = Rpc(self.rpc_url, timeout=2)
        for _ in range(200):
            try:
                rpc("eth_chainId")
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"anvil exited with code {self.proc.returncode}")
                time.sleep(0.05)
        else:
            self.__exit__(None, None, None)
            raise RuntimeError("anvil did not start")
        self.contract = deploy(self.rpc_url)["contract"]
        assert has_contract(self.rpc_url, self.contract)
        return self

    def chain(self, private_key=None):
        from .evm import EvmChain

        return EvmChain(self.rpc_url, self.contract, private_key)

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
