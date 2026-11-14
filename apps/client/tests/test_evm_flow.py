"""Two devices on one EVM chain (a throwaway anvil node), each with its own workspace and
attestor: clone from the chain alone, race two approved PRs on the same parent, recover
the loser by sync + rebase, and verify both clones against the chain.

Skipped when anvil is not installed (`python demo/demo.py chain install`)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from attestor.config import Settings
from attestor.main import app as attestor_app, get_attestor
from attestor.review import Attestor
from dosr_chain import StaleParentError
from dosr_chain.dev import Anvil, find_anvil
from dosr_client.attestor import AttestorClient
from dosr_client.chain_sync import clone_from_chain, land, sync, verify
from dosr_client.config import ClientProfile, DosrRepo
from dosr_client.ipfs import IpfsStore, LocalStore, P2PStore
from dosr_client.p2p import P2PError, _check_against_shared_chain, publish
from dosr_client.pipeline import SubmitOptions, submit
from dosr_client.repo_ops import create_repository
from dosr_protocol.policy import build_policy

pytestmark = pytest.mark.skipif(find_anvil() is None, reason="anvil not installed (demo.py chain install)")


@pytest.fixture(scope="module")
def node():
    with Anvil() as n:
        yield n


class Device:
    def __init__(self, root: Path, node, store, profile: ClientProfile):
        self.workspace = root / "ws"
        self.workspace.mkdir(parents=True)
        self.node, self.store, self.profile = node, store, profile
        self.chain = node.chain(profile.chain_key())
        self.settings = Settings(chain_kind="evm", chain_rpc_url=node.rpc_url, chain_contract=node.contract,
                                 cache_dir=root / "attestor-cache")

    def attestor(self) -> AttestorClient:
        attestor_app.dependency_overrides[get_attestor] = lambda: Attestor(self.settings)
        return AttestorClient("http://testserver", http=TestClient(attestor_app))

    def repo(self, name="shared") -> DosrRepo:
        return DosrRepo(self.workspace / name)

    def edit(self, rel, text, name="shared"):
        p = self.workspace / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def submit(self, message, register=True, chain=None, name="shared"):
        return submit(self.repo(name), self.profile, self.attestor(), chain or self.chain,
                      SubmitOptions(message=message, register=register),
                      publisher=lambda r: publish(r, self.chain, self.store, self.profile)).to_dict()


@pytest.fixture
def devices(tmp_path, node):
    store = P2PStore(IpfsStore("http://127.0.0.1:9"), LocalStore(tmp_path / "store"))  # offline IPFS -> local store
    a = Device(tmp_path / "a", node, store, ClientProfile(id="bob", name="Bob", email="bob@dosr.demo", chain_account=3))
    b = Device(tmp_path / "b", node, store,
               ClientProfile(id="carol", name="Carol", email="carol@dosr.demo", chain_account=4))
    yield a, b
    attestor_app.dependency_overrides.clear()


def make_shared(a: Device, b: Device):
    create_repository(a.workspace / "shared", name="shared", policy=build_policy("standard", repo_name="shared"),
                      profile=a.profile, chain=a.chain, seed_dir=None)
    a.edit("app.py", "print('hello')\n")
    assert a.submit("Add app")["outcome"] == "approved"
    res = clone_from_chain(b.workspace, b.chain, b.store, a.repo().config["repo_id"])
    assert res["action"] == "clone"
    return res


class Racing:
    """Lands a rival's approved transition just before ours: the race, deterministically."""

    def __init__(self, inner, rival_response):
        self.inner, self.rival = inner, rival_response

    def accept_transition(self, response, now=None):
        self.inner.accept_transition(self.rival)
        return self.inner.accept_transition(response)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_clone_from_the_chain_alone(devices):
    a, b = devices
    res = make_shared(a, b)
    assert res["canonical_head"] == a.chain.get_head(a.repo().config["repo_id"])
    assert res["registered"] is False  # a shared chain never registers on import
    for dev in (a, b):
        v = verify(dev.repo(), dev.chain)
        assert v["status"] == "in-sync", v["checks"]
        assert all(c["ok"] for c in v["checks"]), v["checks"]
        assert [h["signature_ok"] for h in v["history"][1:]] == [True]


def test_race_loser_syncs_rebases_and_lands(devices):
    a, b = devices
    make_shared(a, b)
    repo_id = a.repo().config["repo_id"]

    a.edit("README.md", "# shared\n\nUsage: python app.py\n")
    bob = a.submit("Docs", register=False)
    b.edit("CHANGELOG.md", "- app\n")
    carol = b.submit("Changelog", register=False)
    assert bob["outcome"] == carol["outcome"] == "approved"
    assert bob["info"]["parent_git_oid"] == carol["info"]["parent_git_oid"]

    land(a.repo(), a.chain, bob["request_id"])
    with pytest.raises(StaleParentError) as e:
        land(b.repo(), b.chain, carol["request_id"])
    assert e.value.head == bob["info"]["candidate_git_oid"] == a.chain.get_head(repo_id)

    # B is behind the chain and does not even have the winning commit yet.
    v = verify(b.repo(), b.chain)
    assert v["status"] == "behind" and v["relation"]["state"] == "missing"
    # Only an older snapshot is on the chain yet: sync gets that far and says what is missing.
    early = sync(b.repo(), b.chain, b.store, profile=b.profile)
    assert early["status"] == "partial" and any("ask the author" in w for w in early["warnings"])
    publish(a.repo(), a.chain, a.store, a.profile)  # Bob publishes: snapshot on the chain
    res = sync(b.repo(), b.chain, b.store, profile=b.profile)
    assert res["rebased"]["commits"] == 1
    assert res["verify"]["relation"]["state"] == "ahead"

    again = b.submit(None)  # the rebased commit, reviewed again
    assert again["outcome"] == "approved", again["errors"]
    assert again["info"]["parent_git_oid"] == bob["info"]["candidate_git_oid"]

    sync(a.repo(), a.chain, a.store, profile=a.profile)  # Bob fast-forwards to Carol's commit
    heads = {dev.repo().git.head() for dev in (a, b)}
    assert len(heads) == 1
    for dev in (a, b):
        assert verify(dev.repo(), dev.chain)["status"] == "in-sync"
    assert (a.workspace / "shared" / "CHANGELOG.md").exists() and (b.workspace / "shared" / "README.md").exists()


def test_pipeline_reports_a_lost_race_as_conflict(devices):
    a, b = devices
    make_shared(a, b)
    a.edit("one.txt", "1\n")
    rival = a.submit("Rival", register=False)
    resp = (Path(rival["artifacts_dir"]) / "response.json").read_text("utf-8")
    import json

    b.edit("two.txt", "2\n")
    snap = b.submit("Mine", chain=Racing(b.chain, json.loads(resp)))
    assert snap["outcome"] == "conflict"
    assert snap["info"]["chain_conflict"]["head"] == rival["info"]["candidate_git_oid"]
    assert next(s for s in snap["steps"] if s["id"] == "publish")["status"] == "skipped"


def test_share_for_a_head_the_chain_never_accepted_is_refused(devices):
    a, b = devices
    make_shared(a, b)
    cfg = a.repo().config
    onchain = a.chain.get_repository(cfg["repo_id"])
    m = {"repo": cfg, "canonical_head": "sha1:" + "7" * 40, "policy_hash": onchain["policy_hash"],
         "genesis_git_oid": onchain["genesis_git_oid"], "transitions": []}
    with pytest.raises(P2PError, match="never accepted"):
        _check_against_shared_chain(a.chain, a.repo().git, m, None)
    with pytest.raises(P2PError, match="genesis/policy differs"):
        _check_against_shared_chain(a.chain, a.repo().git, {**m, "policy_hash": "0x" + "1" * 64}, None)
