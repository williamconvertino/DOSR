// SPDX-License-Identifier: MIT
pragma solidity 0.8.38;

/// @title DOSR repository registry (protocol v0.1)
/// @notice Holds the canonical Git HEAD of every DOSR repository and moves it only
/// through attestations signed by an attestor the repository's genesis policy lists.
///
/// The contract does cheap, deterministic checks only (docs/ARCHITECTURE.md):
/// approved attestor, EIP-712 signature, approval, expiry, genesis policy hash, and
/// `parent == HEAD`. Source code, prompts and model output stay off-chain; the
/// attestation commits to them by SHA-256 hash (docs/HASHING.md).
///
/// Conflicts: two approved PRs built on the same parent are both valid attestations,
/// but only the first one mined moves HEAD. The second then has a stale parent and
/// reverts with `StaleParent`, so every client that reads the chain sees one history.
contract DOSRRegistry {
    string public constant NAME = "DOSR";
    string public constant VERSION = "0.1";

    uint8 public constant FORMAT_SHA1 = 1;
    uint8 public constant FORMAT_SHA256 = 2;

    // keccak256 of the type strings in dosr_protocol/eip712.py (checked against
    // examples/wire/6. eip712-test-vector.json by packages/chain/tests).
    bytes32 public constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant ATTESTATION_TYPEHASH = keccak256(
        "Attestation(bytes32 repoId,bytes32 policyHash,bytes32 parentGitOid,bytes32 candidateGitOid,"
        "bytes32 bundleSha256,string bundleCid,string provider,string model,string modelVersion,bool approved,"
        "bytes32 requestHash,bytes32 reviewInputHash,bytes32 responseHash,uint64 issuedAt,uint64 expiresAt,"
        "string attestorKeyId)"
    );

    /// Field order and types match ATTESTATION_TYPEHASH / dosr_protocol.eip712.ATTESTATION_FIELDS.
    struct Attestation {
        bytes32 repoId;
        bytes32 policyHash;
        bytes32 parentGitOid;
        bytes32 candidateGitOid;
        bytes32 bundleSha256;
        string bundleCid;
        string provider;
        string model;
        string modelVersion;
        bool approved;
        bytes32 requestHash;
        bytes32 reviewInputHash;
        bytes32 responseHash;
        uint64 issuedAt;
        uint64 expiresAt;
        string attestorKeyId;
    }

    struct AttestorKey {
        string keyId;
        address signer;
    }

    struct Transition {
        bytes32 parentGitOid;
        bytes32 candidateGitOid;
        bytes32 attestationDigest;
        address attestor;
        address submitter;
        uint64 blockNumber;
        uint64 timestamp;
    }

    struct Repository {
        string name;
        bytes32 policyHash;
        bytes32 genesisGitOid;
        bytes32 head;
        uint8 objectFormat;
        address creator;
        uint64 registeredBlock;
        uint64 registeredAt;
        uint64 headBlock;
        // Data-availability hint: where the Git objects of a canonical HEAD can be
        // fetched (an IPFS CID of a DOSR share manifest). Untrusted; clients verify it.
        string snapshotCid;
        bytes32 snapshotHead;
    }

    mapping(bytes32 => Repository) private _repos;
    mapping(bytes32 => AttestorKey[]) private _attestors;
    mapping(bytes32 => mapping(bytes32 => address)) private _signerOf; // repo => keccak(keyId) => signer
    mapping(bytes32 => Transition[]) private _transitions;
    // repo => commit => position in the canonical history (genesis = 1, then 2, 3, ...); 0 = never canonical
    mapping(bytes32 => mapping(bytes32 => uint256)) private _canonicalIndex;
    bytes32[] private _repoIds;

    event RepositoryRegistered(
        bytes32 indexed repoId, string name, bytes32 policyHash, bytes32 genesisGitOid, uint8 objectFormat,
        address indexed creator
    );
    event TransitionAccepted(
        bytes32 indexed repoId, uint256 indexed seq, bytes32 parentGitOid, bytes32 candidateGitOid,
        address attestor, bytes32 attestationDigest, string bundleCid, address submitter
    );
    event SnapshotPublished(bytes32 indexed repoId, bytes32 indexed head, string cid, address publisher);

    error AlreadyRegistered(bytes32 repoId);
    error UnknownRepository(bytes32 repoId);
    error InvalidRegistration(string reason);
    error PolicyMismatch(bytes32 expected, bytes32 got);
    error UnknownAttestor(string keyId);
    error BadSignature(address recovered, address expected);
    error NotApproved();
    error Expired(uint64 expiresAt, uint256 nowTs);
    error NotYetValid(uint64 issuedAt, uint256 nowTs);
    error StaleParent(bytes32 head, bytes32 parent);
    error NoChange();
    error AlreadyCanonical(bytes32 candidate);
    error NotCanonicalHead(bytes32 head, bytes32 given);

    // ------------------------------------------------------------------ writes

    /// Genesis. The registrant supplies the policy hash and the attestor keys listed in
    /// that policy; clients check both against the genesis commit's .dosr/policy.json.
    function registerRepository(
        bytes32 repoId,
        string calldata name,
        bytes32 policyHash,
        bytes32 genesisGitOid,
        uint8 objectFormat,
        AttestorKey[] calldata attestors
    ) external {
        if (_repos[repoId].registeredBlock != 0) revert AlreadyRegistered(repoId);
        if (repoId == bytes32(0) || policyHash == bytes32(0) || genesisGitOid == bytes32(0)) {
            revert InvalidRegistration("zero repo id, policy hash or genesis");
        }
        if (objectFormat != FORMAT_SHA1 && objectFormat != FORMAT_SHA256) {
            revert InvalidRegistration("unknown git object format");
        }
        if (attestors.length == 0 || attestors.length > 16) revert InvalidRegistration("need 1..16 attestors");

        Repository storage r = _repos[repoId];
        r.name = name;
        r.policyHash = policyHash;
        r.genesisGitOid = genesisGitOid;
        r.head = genesisGitOid;
        r.objectFormat = objectFormat;
        r.creator = msg.sender;
        r.registeredBlock = uint64(block.number);
        r.registeredAt = uint64(block.timestamp);
        r.headBlock = uint64(block.number);
        for (uint256 i = 0; i < attestors.length; i++) {
            bytes32 k = keccak256(bytes(attestors[i].keyId));
            if (attestors[i].signer == address(0) || _signerOf[repoId][k] != address(0)) {
                revert InvalidRegistration("zero or duplicate attestor");
            }
            _signerOf[repoId][k] = attestors[i].signer;
            _attestors[repoId].push(attestors[i]);
        }
        _canonicalIndex[repoId][genesisGitOid] = 1;
        _repoIds.push(repoId);
        emit RepositoryRegistered(repoId, name, policyHash, genesisGitOid, objectFormat, msg.sender);
    }

    /// Moves HEAD from `att.parentGitOid` to `att.candidateGitOid`. Anyone may submit
    /// (the contributor pays gas); only the attestor's signature authorizes it.
    function acceptTransition(Attestation calldata att, bytes calldata signature) external returns (uint256 seq) {
        Repository storage r = _repos[att.repoId];
        if (r.registeredBlock == 0) revert UnknownRepository(att.repoId);
        if (att.policyHash != r.policyHash) revert PolicyMismatch(r.policyHash, att.policyHash);

        address expected = _signerOf[att.repoId][keccak256(bytes(att.attestorKeyId))];
        if (expected == address(0)) revert UnknownAttestor(att.attestorKeyId);
        bytes32 digest = hashAttestation(att);
        address recovered = _recover(digest, signature);
        if (recovered != expected) revert BadSignature(recovered, expected);

        if (!att.approved) revert NotApproved();
        if (att.expiresAt <= block.timestamp) revert Expired(att.expiresAt, block.timestamp);
        // A little slack: the attestor's clock may run slightly ahead of block time.
        if (att.issuedAt > block.timestamp + 300) revert NotYetValid(att.issuedAt, block.timestamp);
        if (att.parentGitOid != r.head) revert StaleParent(r.head, att.parentGitOid);
        if (att.candidateGitOid == att.parentGitOid) revert NoChange();
        if (_canonicalIndex[att.repoId][att.candidateGitOid] != 0) revert AlreadyCanonical(att.candidateGitOid);

        r.head = att.candidateGitOid;
        r.headBlock = uint64(block.number);
        _transitions[att.repoId].push(Transition({
            parentGitOid: att.parentGitOid,
            candidateGitOid: att.candidateGitOid,
            attestationDigest: digest,
            attestor: recovered,
            submitter: msg.sender,
            blockNumber: uint64(block.number),
            timestamp: uint64(block.timestamp)
        }));
        seq = _transitions[att.repoId].length;
        _canonicalIndex[att.repoId][att.candidateGitOid] = seq + 1;
        emit TransitionAccepted(
            att.repoId, seq, att.parentGitOid, att.candidateGitOid, recovered, digest, att.bundleCid, msg.sender
        );
    }

    /// Records where the Git objects for the *current* canonical HEAD can be fetched.
    /// Anyone may publish (it is only a hint; importers verify the bundle against HEAD).
    function publishSnapshot(bytes32 repoId, bytes32 head, string calldata cid) external {
        Repository storage r = _repos[repoId];
        if (r.registeredBlock == 0) revert UnknownRepository(repoId);
        if (head != r.head) revert NotCanonicalHead(r.head, head);
        if (bytes(cid).length == 0 || bytes(cid).length > 128) revert InvalidRegistration("bad cid length");
        r.snapshotCid = cid;
        r.snapshotHead = head;
        emit SnapshotPublished(repoId, head, cid, msg.sender);
    }

    // ------------------------------------------------------------------ reads

    function getRepository(bytes32 repoId) external view returns (Repository memory repo, uint256 transitionCount) {
        repo = _repos[repoId];
        if (repo.registeredBlock == 0) revert UnknownRepository(repoId);
        transitionCount = _transitions[repoId].length;
    }

    function getHead(bytes32 repoId) external view returns (bytes32) {
        if (_repos[repoId].registeredBlock == 0) revert UnknownRepository(repoId);
        return _repos[repoId].head;
    }

    function isRegistered(bytes32 repoId) external view returns (bool) {
        return _repos[repoId].registeredBlock != 0;
    }

    function getAttestors(bytes32 repoId) external view returns (AttestorKey[] memory) {
        return _attestors[repoId];
    }

    function getTransitions(bytes32 repoId, uint256 offset, uint256 limit)
        external
        view
        returns (Transition[] memory out)
    {
        Transition[] storage all = _transitions[repoId];
        if (offset >= all.length) return new Transition[](0);
        uint256 n = all.length - offset < limit ? all.length - offset : limit;
        out = new Transition[](n);
        for (uint256 i = 0; i < n; i++) {
            out[i] = all[offset + i];
        }
    }

    /// Position of `gitOid` in the canonical history (genesis = 1); 0 if it was never canonical.
    function canonicalIndex(bytes32 repoId, bytes32 gitOid) external view returns (uint256) {
        return _canonicalIndex[repoId][gitOid];
    }

    function repositoryCount() external view returns (uint256) {
        return _repoIds.length;
    }

    function repositoryIds(uint256 offset, uint256 limit) external view returns (bytes32[] memory out) {
        if (offset >= _repoIds.length) return new bytes32[](0);
        uint256 n = _repoIds.length - offset < limit ? _repoIds.length - offset : limit;
        out = new bytes32[](n);
        for (uint256 i = 0; i < n; i++) {
            out[i] = _repoIds[offset + i];
        }
    }

    // ------------------------------------------------------------------ EIP-712

    function domainSeparator() public view returns (bytes32) {
        return keccak256(abi.encode(
            DOMAIN_TYPEHASH, keccak256(bytes(NAME)), keccak256(bytes(VERSION)), block.chainid, address(this)
        ));
    }

    function structHash(Attestation calldata a) public pure returns (bytes32) {
        // Two halves only to stay under the stack limit; the bytes equal one abi.encode.
        bytes memory head = abi.encode(
            ATTESTATION_TYPEHASH, a.repoId, a.policyHash, a.parentGitOid, a.candidateGitOid, a.bundleSha256,
            keccak256(bytes(a.bundleCid)), keccak256(bytes(a.provider)), keccak256(bytes(a.model))
        );
        bytes memory tail = abi.encode(
            keccak256(bytes(a.modelVersion)), a.approved, a.requestHash, a.reviewInputHash, a.responseHash,
            a.issuedAt, a.expiresAt, keccak256(bytes(a.attestorKeyId))
        );
        return keccak256(bytes.concat(head, tail));
    }

    function hashAttestation(Attestation calldata a) public view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash(a)));
    }

    function _recover(bytes32 digest, bytes calldata sig) private pure returns (address) {
        if (sig.length != 65) return address(0);
        bytes32 r = bytes32(sig[0:32]);
        bytes32 s = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        if (v < 27) v += 27;
        // Reject malleable (high-s) signatures, as OpenZeppelin's ECDSA does.
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0 || (v != 27 && v != 28)) {
            return address(0);
        }
        return ecrecover(digest, v, r, s);
    }
}
