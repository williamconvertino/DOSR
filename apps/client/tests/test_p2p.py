"""Sharing a repository between two devices (each with its own workspace, mock chain,
and attestor), through the content store. Uses the local-store backend so no IPFS
node is needed; the IPFS backend only changes where the bytes live."""

import json
from pathlib import Path

import pytest

from attestor.config import Settings
from attestor.main import app as attestor_app, get_attestor
from attestor.review import Attestor
from dosr_chain import MockChain
from dosr_client.attestor import AttestorClient
from dosr_client.config import ClientProfile, DosrRepo
from dosr_client.ipfs import IpfsStore, LocalStore, P2PStore
from dosr_client.p2p import P2PError, fetch_manifest, import_share, parse_share_code, publish
from dosr_client.pipeline import SubmitOptions, submit
from dosr_client.repo_ops import create_repository
from dosr_protocol.policy import build_policy
from fastapi.testclient import TestClient


class Device:
    def __init__(self, root: Path, store: P2PStore):
        self.workspace = root / "ws"
        self.chain = MockChain(root / "chain" / "state.json")
        self.store = store
        self.cache = root / "attestor-cache"

    def attestor(self) -> AttestorClient:
        # Each device runs its own attestor, reading its own chain (same dev signing key).
        settings = Settings(chain_state=self.chain.state_path, cache_dir=self.cache)
        attestor_app.dependency_overrides[get_attestor] = lambda: Attestor(settings)
        return AttestorClient("http://testserver", http=TestClient(attestor_app))

    def repo(self, name) -> DosrRepo:
        return DosrRepo(self.workspace / name)

    def submit(self, name, profile, message, register=True):
        sub = submit(self.repo(name), profile, self.attestor(), self.chain,
                     SubmitOptions(message=message, register=register))
        return sub.to_dict()


@pytest.fixture
def devices(tmp_path):
    # Offline IPFS address: the store falls back to the shared local directory.
    store = P2PStore(IpfsStore("http://127.0.0.1:9"), LocalStore(tmp_path / "store"))
    yield Device(tmp_path / "a", store), Device(tmp_path / "b", store)
    attestor_app.dependency_overrides.clear()


@pytest.fixture
def bob():
    return ClientProfile(id="bob", name="Bob", email="bob@dosr.demo")


def make_repo(dev: Device, profile, name="shared"):
    create_repository(dev.workspace / name, name=name, policy=build_policy("standard", repo_name=name),
                      profile=profile, chain=dev.chain, description="p2p test")
    (dev.workspace / name / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return dev.submit(name, profile, "Add app.py")


def test_clone_then_pull_round_trip(devices, alice, bob):
    a, b = devices
    assert make_repo(a, alice)["outcome"] == "approved"
    head_a = a.chain.get_head(a.repo("shared").config["repo_id"])

    pub = publish(a.repo("shared"), a.chain, a.store, alice, device="laptop-a")
    assert pub["backend"] == "local" and pub["share"].startswith("local:")
    assert pub["accepted_transitions"] == 1 and pub["canonical_head"] == head_a

    # Device B: clone. Genesis registered on B's chain, the transition replayed (signature checked).
    res = import_share(b.workspace, b.chain, b.store, pub["share"], expect_head=head_a.split(":")[1][:10])
    assert res["action"] == "clone" and res["registered"] and len(res["applied"]) == 1
    repo_b = b.repo(res["id"])
    assert b.chain.get_head(repo_b.config["repo_id"]) == head_a
    assert (repo_b.path / "app.py").read_text() == "print('hi')\n"
    assert repo_b.git.head() == head_a.split(":")[1]

    # B proposes a change, reviewed by B's own attestor against B's chain.
    (repo_b.path / "app.py").write_text("print('hello from b')\n", encoding="utf-8")
    assert b.submit(res["id"], bob, "Greet from B")["outcome"] == "approved"
    pub_b = publish(repo_b, b.chain, b.store, bob)
    assert pub_b["accepted_transitions"] == 2

    # Device A pulls: B's transition is verified and applied, working copy fast-forwards.
    res_a = import_share(a.workspace, a.chain, a.store, pub_b["share"])
    assert res_a["action"] == "pull" and res_a["status"] == "updated" and len(res_a["applied"]) == 1
    assert a.chain.get_head(a.repo("shared").config["repo_id"]) == pub_b["canonical_head"]
    assert (a.workspace / "shared" / "app.py").read_text() == "print('hello from b')\n"

    # Pulling again is a no-op; A can now publish everything (its own + imported attestations).
    assert import_share(a.workspace, a.chain, a.store, pub_b["share"])["status"] == "up-to-date"
    assert publish(a.repo("shared"), a.chain, a.store, alice)["accepted_transitions"] == 2
    # An older share code changes nothing.
    assert import_share(a.workspace, a.chain, a.store, pub["share"])["status"] == "older"


def test_forged_transition_is_rejected(devices, alice):
    a, b = devices
    make_repo(a, alice)
    m = fetch_manifest(a.store, publish(a.repo("shared"), a.chain, a.store, alice)["share"])
    m["transitions"][0]["signature"]["value"] = "0x" + "11" * 65
    forged = a.store.add(json.dumps(m).encode())
    with pytest.raises(P2PError, match="failed verification"):
        import_share(b.workspace, b.chain, b.store, forged)
    assert not any(b.workspace.iterdir())  # half-made clone removed


def test_tampered_bundle_and_bad_codes(devices, alice):
    a, b = devices
    make_repo(a, alice)
    m = fetch_manifest(a.store, publish(a.repo("shared"), a.chain, a.store, alice)["share"])
    m["bundle"]["id"] = a.store.add(b"not a bundle")
    with pytest.raises(P2PError, match="digest"):
        import_share(b.workspace, b.chain, b.store, a.store.add(json.dumps(m).encode()))
    with pytest.raises(P2PError, match="not a DOSR share manifest"):
        import_share(b.workspace, b.chain, b.store, a.store.add(b'{"hello": 1}'))
    with pytest.raises(P2PError):
        parse_share_code("hello world")
    assert parse_share_code("https://ipfs.io/ipfs/bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi") \
        == "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"


def test_conflicting_heads_are_reported(devices, alice, bob):
    a, b = devices
    make_repo(a, alice)
    share = publish(a.repo("shared"), a.chain, a.store, alice)["share"]
    rid = import_share(b.workspace, b.chain, b.store, share)["id"]
    # Both devices accept a different change on the same parent.
    (a.workspace / "shared" / "a.txt").write_text("a\n")
    assert a.submit("shared", alice, "A's change")["outcome"] == "approved"
    (b.workspace / rid / "b.txt").write_text("b\n")
    assert b.submit(rid, bob, "B's change")["outcome"] == "approved"
    share_b = publish(b.repo(rid), b.chain, b.store, bob)["share"]
    before = a.chain.get_head(a.repo("shared").config["repo_id"])
    with pytest.raises(P2PError, match="conflict"):
        import_share(a.workspace, a.chain, a.store, share_b)
    assert a.chain.get_head(a.repo("shared").config["repo_id"]) == before

    # Dev escape hatch: A adopts B's history; its own change is dropped from main.
    res = import_share(a.workspace, a.chain, a.store, share_b, replace=True)
    assert res["status"] == "replaced" and len(res["applied"]) == 2  # replayed from genesis
    repo_a = a.repo("shared")
    assert a.chain.get_head(repo_a.config["repo_id"]) == res["canonical_head"]
    assert repo_a.git.head() == res["canonical_head"].split(":")[1]
    assert (repo_a.path / "b.txt").exists() and not (repo_a.path / "a.txt").exists()


def test_half_valid_share_leaves_chain_untouched(devices, alice, bob):
    a, b = devices
    make_repo(a, alice)
    (a.workspace / "shared" / "two.txt").write_text("2\n")
    assert a.submit("shared", alice, "Second change")["outcome"] == "approved"
    m = fetch_manifest(a.store, publish(a.repo("shared"), a.chain, a.store, alice)["share"])
    m["transitions"][1]["signature"]["value"] = "0x" + "22" * 65  # first transition valid, second forged
    with pytest.raises(P2PError, match="failed verification"):
        import_share(b.workspace, b.chain, b.store, a.store.add(json.dumps(m).encode()))
    assert b.chain.list_repositories() == {}
