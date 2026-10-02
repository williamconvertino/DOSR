# DOSR Contracts

Prototype contract responsibilities:

- Register repository genesis state and immutable `policyHash`.
- Track canonical `HEAD` for each repository.
- Verify attestor signatures and signed attestation fields.
- Require attested `parent` to equal current `HEAD`.
- Require approval and a non-expired attestation.
- Update `HEAD` atomically and emit a transition event.

Source code, prompts, model responses, and Git objects remain off-chain.
