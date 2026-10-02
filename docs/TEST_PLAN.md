# Initial Test Matrix

1. Happy path: parent is HEAD, bundle matches candidate, model approves, signature valid.
2. Stale parent: candidate was reviewed against an old HEAD; contract rejects.
3. Swapped candidate: signature is valid but candidate differs from attestation; reject.
4. Swapped bundle/CID: bundle digest/CID differs from signed attestation; reject client-side/on submission policy.
5. Modified policy: supplied policy hash differs from genesis policy hash; attestor rejects.
6. Unapproved model: attestor rejects.
7. Unapproved attestor: contract rejects.
8. Expired attestation: contract rejects.
9. Replayed attestation on another chain/contract/repository: EIP-712 domain/repo ID prevents acceptance.
10. Malicious IPFS peer: fetched data does not contain canonical Git OID; client rejects and tries another peer.
