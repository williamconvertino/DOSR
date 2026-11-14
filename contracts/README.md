# DOSR Contracts

`src/DOSRRegistry.sol` is the on-chain half of DOSR: the canonical `HEAD` of every
repository, moved only by attestations signed by an attestor the repository's genesis
policy trusts. Source code, prompts, model output and Git objects stay off-chain; the
attestation commits to them by SHA-256 hash ([docs/HASHING.md](../docs/HASHING.md)).

## What is stored per repository

| Field | Set by | Notes |
|---|---|---|
| `name`, `creator`, `registeredBlock` | `registerRepository` | |
| `policyHash` | `registerRepository` | SHA-256 of the canonical `.dosr/policy.json`; immutable |
| `genesisGitOid` | `registerRepository` | the root commit (SHA-1 OIDs right-padded to `bytes32`) |
| attestor keys (`keyId → signer`) | `registerRepository` | the `attestors` of the policy; immutable |
| `head`, `headBlock` | `acceptTransition` | the canonical HEAD |
| transitions `[parent, candidate, digest, attestor, submitter, block, time]` | `acceptTransition` | the whole canonical history, in order |
| `canonicalIndex(commit)` | `acceptTransition` | position in the history (genesis = 1), 0 = never canonical |
| `snapshotCid`, `snapshotHead` | `publishSnapshot` | where to fetch the Git objects of HEAD (an IPFS share-manifest CID); a hint, verified by importers |

## Accepting a transition

`acceptTransition(Attestation att, bytes signature)` (anyone may send it; the
contributor pays gas) checks, in order:

1. the repository is registered and `att.policyHash` is its genesis policy hash;
2. `att.attestorKeyId` is one of the registered keys, and the EIP-712 signature over
   `att` (domain: name `DOSR`, version `0.1`, this chain id, this contract) recovers to
   that key's address (high-s signatures rejected);
3. `att.approved`, `att.expiresAt > block.timestamp`, `att.issuedAt` not in the future;
4. **`att.parentGitOid == head`**, and the candidate differs from the parent and was never
   canonical before.

Then `head = att.candidateGitOid`, the transition is appended, and
`TransitionAccepted(repoId, seq, parent, candidate, attestor, digest, bundleCid, submitter)`
is emitted. The digest is the EIP-712 digest the attestor signed, so it identifies the
transition; the client recomputes it with `dosr_protocol.eip712.attestation_digest`.

The struct hash and digest are computed exactly as `dosr_protocol/eip712.py` does;
`packages/chain/tests/test_evm_chain.py` checks the contract against
[the EIP-712 test vector](../examples/wire/6.%20eip712-test-vector.json).

## Conflicts: one canonical history

The attestor checks `parent == HEAD` when it reviews, but two contributors can both be
reviewed against the same HEAD and both receive valid, approved attestations. Only the
chain decides between them: the first `acceptTransition` mined moves HEAD, and every
later one for that parent reverts with `StaleParent(head, parent)`. Transactions in the
same block are ordered too, so even a same-block race has exactly one winner. Every
client that reads the contract therefore sees one history.

The loser's attestation can never land (it names a parent that is no longer HEAD). Its
client recovers by syncing to the new HEAD (fetching its Git objects through the
snapshot), rebasing its commit, and submitting again; the attestor reviews the rebased
commit against the new HEAD (`dosr sync`, then `dosr submit`; GUI: *Chain* tab →
*Sync & rebase*).

Reorgs: on a real network a client would treat HEAD as final only after some
confirmations (`DOSR_CONFIRMATIONS`, which makes reads lag the tip). The local dev chain
has a single node and no reorgs.

## Build, deploy, test

```bash
python contracts/build.py          # downloads solc 0.8.38 once (sha256-checked), writes
                                   #   packages/chain/dosr_chain/contract/DOSRRegistry.json
python contracts/build.py --check  # the committed artifact matches the source?
python demo/demo.py chain          # local anvil node + deploy (see demo/README.md)
python -m dosr_client.cli chain --deploy --rpc-url http://127.0.0.1:8545
```

The compiled artifact is committed, so nothing else needs a compiler. On a fresh dev
chain, the first deployment from dev account 0 always lands at
`0x5FbDB2315678afecb367f032d93F642f64180aa3`, the default contract address everywhere.

Tests (`packages/chain/tests/test_evm_chain.py`, `apps/client/tests/test_evm_flow.py`)
start a throwaway anvil node; they are skipped when anvil is not installed.

## Limitations (prototype)

- Registration is permissionless and first-come: the registrant states the policy hash
  and attestor keys. Clients check both against the genesis commit (`dosr verify`:
  `policy_hash_matches`, `attestors_match_policy`); a squatter that registers someone
  else's random repo id first can only make that id unusable.
- `minimum_approvals` > 1 is not enforced on-chain yet (one attestation per transition).
- The snapshot pointer is a hint anyone can overwrite for the current HEAD; importers
  verify the bundle against the chain, so a bad pointer can delay a sync but not fake one.
