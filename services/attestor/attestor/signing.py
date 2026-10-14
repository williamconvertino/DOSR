"""EIP-712 signing of attestations (typed data shared with the chain: dosr_protocol.eip712)."""

from dosr_protocol.eip712 import typed_data
from eth_account import Account

SCHEME = "eip712-secp256k1"


class Signer:
    def __init__(self, private_key: str):
        self._account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    def sign(self, attestation: dict) -> str:
        signed = Account.sign_typed_data(self._account.key, full_message=typed_data(attestation))
        return "0x" + bytes(signed.signature).hex()
