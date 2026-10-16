import copy
import time

import pytest

from eth_account import Account

from dosr_chain import ChainError, MockChain
from dosr_protocol.canonical import policy_hash
from dosr_protocol.eip712 import typed_data
from dosr_protocol.policy import load_preset

GENESIS = "sha1:" + "1" * 40
CANDIDATE = "sha1:" + "2" * 40
# Hardhat test account #1: the standard preset's local-dev-attestor-1.
ATTESTOR = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")


def sign(att, account=ATTESTOR):
    return "0x" + bytes(Account.sign_typed_data(account.key, full_message=typed_data(att)).signature).hex()


@pytest.fixture
def chain(tmp_path):
    c = MockChain(tmp_path / "state.json")
    c.register_repository(repo_id="0x" + "ab" * 32, name="r", policy=load_preset("standard"),
                          genesis_git_oid=GENESIS, object_format="sha1")
    return c


def response(parent=GENESIS, approved=True, expires=None, **att_overrides):
    now = int(time.time())
    att = {
        "repo_id": "0x" + "ab" * 32,
        "chain_id": 31337,
        "verifying_contract": MockChain.contract,
        "policy_hash": policy_hash(load_preset("standard")),
        "parent_git_oid": parent,
        "candidate_git_oid": CANDIDATE,
        "bundle_sha256": "sha256:" + "b" * 64,
        "bundle_cid": None,
        "provider": "mock",
        "model": "deterministic-reviewer-v1",
        "model_version": "1",
        "approved": approved,
        "request_hash": "0x" + "c" * 64,
        "review_input_hash": "0x" + "e" * 64,
        "response_hash": "0x" + "d" * 64,
        "issued_at": now,
        "expires_at": expires or now + 3600,
        "attestor_key_id": "local-dev-attestor-1",
        **att_overrides,
    }
    return {"attestation": att, "signature": {"signer": ATTESTOR.address, "value": sign(att)}}


def test_accept_advances_head(chain):
    ev = chain.accept_transition(response())
    assert chain.get_head("0x" + "ab" * 32) == CANDIDATE
    assert ev["block"] == 2


def test_replay_is_stale(chain):
    chain.accept_transition(response())
    with pytest.raises(ChainError, match="stale parent"):
        chain.accept_transition(response())


@pytest.mark.parametrize(
    "resp, msg",
    [
        (response(approved=False), "not an approval"),
        (response(expires=1), "expired"),
        (response(chain_id=1), "domain"),
        (response(policy_hash="0x" + "0" * 64), "policy hash"),
        (response(attestor_key_id="someone-else"), "not approved"),
    ],
)
def test_rejections(chain, resp, msg):
    with pytest.raises(ChainError, match=msg):
        chain.accept_transition(copy.deepcopy(resp))


def test_double_register(chain):
    with pytest.raises(ChainError):
        chain.register_repository(repo_id="0x" + "ab" * 32, name="r", policy=load_preset("standard"),
                                   genesis_git_oid=GENESIS, object_format="sha1")


def test_tampered_attestation_is_rejected(chain):
    resp = response()
    resp["attestation"]["candidate_git_oid"] = "sha1:" + "3" * 40  # swap candidate after signing
    with pytest.raises(ChainError, match="signature does not match"):
        chain.accept_transition(resp)


def test_signature_by_someone_else_is_rejected(chain):
    resp = response()
    resp["signature"]["value"] = sign(resp["attestation"], Account.create())  # claims the listed signer
    with pytest.raises(ChainError, match="signature does not match"):
        chain.accept_transition(resp)


def test_malformed_signature_is_rejected(chain):
    resp = response()
    resp["signature"]["value"] = "0x" + "00" * 65
    with pytest.raises(ChainError, match="malformed|does not match"):
        chain.accept_transition(resp)
