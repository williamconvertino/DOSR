import copy
import time

import pytest

from dosr_chain import ChainError, MockChain
from dosr_protocol.canonical import policy_hash
from dosr_protocol.policy import load_preset

GENESIS = "sha1:" + "1" * 40
CANDIDATE = "sha1:" + "2" * 40


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
        "approved": approved,
        "expires_at": expires or now + 3600,
        "attestor_key_id": "local-dev-attestor-1",
        **att_overrides,
    }
    return {"attestation": att, "signature": {"signer": "0x1111111111111111111111111111111111111111"}}


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
