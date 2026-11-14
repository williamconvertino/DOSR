class ChainError(RuntimeError):
    """The chain refused a call, or could not be reached. `reason` is the contract's
    error name when known (e.g. "BadSignature")."""

    def __init__(self, message: str = "", reason: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.tx_hash: str | None = None


class StaleParentError(ChainError):
    """The attested parent is no longer the canonical HEAD: another approved transition
    landed on the same parent first. `head` is the canonical HEAD now."""

    def __init__(self, message: str, head: str):
        super().__init__(message, reason="StaleParent")
        self.head = head
