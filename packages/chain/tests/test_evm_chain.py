"""DOSRRegistry contract + EvmChain adapter, against a throwaway anvil node.

Skipped when anvil is not installed (`python demo/demo.py chain install`, or put anvil
on PATH / set DOSR_ANVIL)."""

import copy
import os
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from eth_account import Account

from dosr_chain import ChainError, StaleParentError
from dosr_chain.dev import Anvil, dev_key, find_anvil
from dosr_chain.evm import load_artifact
from dosr_protocol.canonical import policy_hash
from dosr_protocol.eip712 import attestation_digest, recover_signer, typed_data
from dosr_protocol.policy import load_preset

ROOT = Path(__file__).resolve().parents[3]
VECTOR = json.loads((ROOT / "examples" / "wire" / "6. eip712-test-vector.json").read_text("utf-8"))
ATTESTOR = Account.from_key(dev_key(1))  # the standard preset's local-dev-attestor-1
REPO = "0x" + "ab" * 32
GENESIS = "sha1:" + "1" * 40

pytestmark = pytest.mark.skipif(find_anvil() is None, reason="anvil not installed (demo.py chain install)")


@pytest.fixture(scope="module")
def node():
    with Anvil() as n:
        yield n


@pytest.fixture
def chain(node):
    return node.chain(dev_key(2))


@pytest.fixture
def repo(chain):
    """A freshly registered repository (new id per test: the node is shared)."""
    repo_id = "0x" + os.urandom(32).hex()
    chain.register_repository(repo_id=repo_id, name="r", policy=load_preset("standard"),
                              genesis_git_oid=GENESIS, object_format="sha1")
    return repo_id


def oid(n: int) -> str:
    return "sha1:" + f"{n:040x}"


def response(chain, repo_id, parent=GENESIS, candidate=None, approved=True, expires=None, key=ATTESTOR, **over):
    now = int(time.time())
    att = {
        "repo_id": repo_id,
        "chain_id": chain.chain_id,
        "verifying_contract": chain.contract,
        "policy_hash": policy_hash(load_preset("standard")),
        "parent_git_oid": parent,
        "candidate_git_oid": candidate or oid(int(time.time_ns()) % 10**12 + 7),
        "bundle_sha256": "sha256:" + "b" * 64,
        "bundle_cid": None,
        "provider": "mock",
        "model": "deterministic-reviewer-v1",
        "model_version": "1",
        "approved": approved,
        "request_hash": "0x" + "c" * 64,
        "review_input_hash": "0x" + "e" * 64,
        "response_hash": "0x" + "d" * 64,
        "issued_at": now - 5,
        "expires_at": expires or now + 3600,
        "attestor_key_id": "local-dev-attestor-1",
        **over,
    }
    sig = "0x" + bytes(Account.sign_typed_data(key.key, full_message=typed_data(att)).signature).hex()
    return {"attestation": att, "signature": {"scheme": "eip712-secp256k1", "signer": key.address, "value": sig}}


# ---------------------------------------------------------------- build + EIP-712 agreement


def test_artifact_matches_source():
    out = subprocess.run([sys.executable, str(ROOT / "contracts" / "build.py"), "--check"], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr


def test_deployed_code_is_this_build(chain):
    info = chain.describe()
    assert info["deployed"] and info["code_matches_source"]
    assert info["chain_id"] == 31337


def test_contract_hashes_match_the_python_test_vector(chain):
    # Type hash and struct hash do not depend on the domain: they must equal the vector's.
    assert "0x" + chain._call("ATTESTATION_TYPEHASH")[0].hex() == VECTOR["attestation_type_hash"]
    msg = typed_data(VECTOR["attestation"])["message"]
    from dosr_protocol.eip712 import ATTESTATION_FIELDS

    values = tuple(msg[name] for name, _, _ in ATTESTATION_FIELDS)
    assert "0x" + chain._call("structHash", values)[0].hex() == VECTOR["struct_hash"]
    # The full digest uses this deployment's address: the contract and Python agree on it.
    att = {**VECTOR["attestation"], "verifying_contract": chain.contract}
    assert "0x" + chain._call("hashAttestation", values)[0].hex() == attestation_digest(att)


# ---------------------------------------------------------------- repository state


def test_register_and_read(chain, repo):
    r = chain.get_repository(repo)
    assert r["canonical_head"] == GENESIS and r["genesis_git_oid"] == GENESIS
    assert r["policy_hash"] == policy_hash(load_preset("standard"))
    assert r["attestors"] == [{"key_id": "local-dev-attestor-1", "scheme": "eip712-secp256k1",
                               "address": ATTESTOR.address}]
    assert r["accepted_transition_count"] == 0 and r["events"] == []
    assert r["creator"] == Account.from_key(dev_key(2)).address
    assert repo in chain.list_repositories()
    assert chain.canonical_index(repo, GENESIS) == 1
    assert chain.get_repository("0x" + "00" * 31 + "01") is None


def test_register_twice_rejected(chain, repo):
    with pytest.raises(ChainError, match="already registered"):
        chain.register_repository(repo_id=repo, name="again", policy=load_preset("standard"),
                                  genesis_git_oid=GENESIS, object_format="sha1")


def test_accept_transition_moves_head(chain, repo):
    resp = response(chain, repo, candidate=oid(2))
    ev = chain.accept_transition(resp)
    assert ev["candidate_git_oid"] == oid(2) and ev["seq"] == 1 and ev["tx_hash"]
    assert ev["attestation_digest"] == attestation_digest(resp["attestation"])
    assert ev["attestor"] == ATTESTOR.address
    r = chain.get_repository(repo)
    assert r["canonical_head"] == oid(2) and r["accepted_transition_count"] == 1
    assert r["events"][0]["tx_hash"] == ev["tx_hash"]
    assert chain.canonical_index(repo, oid(2)) == 2


def test_signed_history_is_rebuilt_from_calldata(chain, repo):
    first = response(chain, repo, candidate=oid(10))
    chain.accept_transition(first)
    second = response(chain, repo, parent=oid(10), candidate=oid(11), bundle_cid="bafy-example")
    chain.accept_transition(second)
    rebuilt = chain.signed_transitions(repo)
    assert len(rebuilt) == 2
    for orig, got in zip((first, second), rebuilt):
        assert attestation_digest(got["attestation"]) == attestation_digest(orig["attestation"])
        assert recover_signer(got["attestation"], got["signature"]["value"]) == ATTESTOR.address
    assert rebuilt[1]["attestation"]["bundle_cid"] == "bafy-example"


@pytest.mark.parametrize("mutate, message", [
    (lambda c, r: response(c, r, approved=False), "not an approval"),
    (lambda c, r: response(c, r, expires=int(time.time()) - 10), "expired"),
    (lambda c, r: response(c, r, key=Account.create()), "signature does not match"),
    (lambda c, r: response(c, r, attestor_key_id="someone-else"), "not approved by the repository policy"),
    (lambda c, r: response(c, r, policy_hash="0x" + "99" * 32), "policy hash"),
    (lambda c, r: response(c, r, candidate=GENESIS), "candidate equals parent"),
])
def test_contract_rejects(chain, repo, mutate, message):
    with pytest.raises(ChainError, match=message):
        chain.accept_transition(mutate(chain, repo))
    assert chain.get_head(repo) == GENESIS


def test_tampered_field_breaks_the_signature(chain, repo):
    resp = response(chain, repo)
    resp = copy.deepcopy(resp)
    resp["attestation"]["model"] = "always-approve"  # signed over deterministic-reviewer-v1
    with pytest.raises(ChainError, match="signature does not match"):
        chain.accept_transition(resp)


def test_other_domain_rejected_before_sending(chain, repo):
    resp = response(chain, repo, verifying_contract="0x4444444444444444444444444444444444444444")
    with pytest.raises(ChainError, match="different chain/contract"):
        chain.accept_transition(resp)


def test_stale_parent(chain, repo):
    chain.accept_transition(response(chain, repo, candidate=oid(20)))
    with pytest.raises(StaleParentError) as e:
        chain.accept_transition(response(chain, repo, parent=GENESIS, candidate=oid(21)))
    assert e.value.head == oid(20)


def test_race_in_one_block_has_one_winner(node, repo):
    """Two approved transitions on the same parent, mined in the same block: exactly one
    lands; the other reverts with StaleParent and the head is the winner's."""
    a, b = node.chain(dev_key(3)), node.chain(dev_key(4))
    ra, rb = response(a, repo, candidate=oid(30)), response(b, repo, candidate=oid(31))
    a.set_automine(False)
    try:
        ta, tb = a.send_transition(ra), b.send_transition(rb)
        a.mine()
    finally:
        a.set_automine(True)
    results = {}
    for name, c, tx, r in (("a", a, ta, ra), ("b", b, tb, rb)):
        try:
            results[name] = c.transition_result(tx, r)
        except StaleParentError as e:
            results[name] = e
    winners = [k for k, v in results.items() if isinstance(v, dict)]
    losers = [k for k, v in results.items() if isinstance(v, StaleParentError)]
    assert len(winners) == 1 and len(losers) == 1
    win = results[winners[0]]
    assert results[losers[0]].head == win["candidate_git_oid"]
    assert a.get_head(repo) == win["candidate_git_oid"]
    assert int(a._s["rpc"]("eth_getTransactionReceipt", ta)["blockNumber"], 16) == \
        int(a._s["rpc"]("eth_getTransactionReceipt", tb)["blockNumber"], 16)
    # The explorer shows the loser's revert reason.
    act = a.activity(limit=10)
    reverted = [t for t in act["transactions"] if t["status"] == "reverted"]
    assert reverted and reverted[0]["error"]["name"] == "StaleParent"


def test_snapshot_only_for_current_head(chain, repo):
    with pytest.raises(ChainError, match="not the canonical HEAD"):
        chain.publish_snapshot(repo, oid(99), "bafy-not-head")
    chain.publish_snapshot(repo, GENESIS, "bafy-genesis")
    r = chain.get_repository(repo)
    assert r["snapshot"] == {"cid": "bafy-genesis", "head": GENESIS}
    assert chain.snapshots(repo)[-1]["cid"] == "bafy-genesis"


def test_artifact_has_expected_interface():
    names = {i["name"] for i in load_artifact()["abi"] if i["type"] in ("function", "event", "error")}
    for n in ("registerRepository", "acceptTransition", "publishSnapshot", "getRepository", "getTransitions",
              "TransitionAccepted", "StaleParent", "BadSignature"):
        assert n in names
