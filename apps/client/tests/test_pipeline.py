"""End-to-end: create repo -> edit -> submit to the real attestor app -> decision."""

import json

import pytest

from dosr_client.config import DosrRepo
from dosr_client.pipeline import SubmitOptions, submit
from dosr_client.repo_ops import create_repository, repo_status
from dosr_protocol.policy import build_policy


def make_repo(tmp_path, chain, profile, preset="standard", **policy_overrides):
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    created = create_repository(
        tmp_path / "repo",
        name="calc",
        description="test repo",
        policy=build_policy(preset, repo_name="calc", **policy_overrides),
        profile=profile,
        chain=chain,
        seed_dir=seed,
    )
    return DosrRepo(created["path"]), created


def edit(repo, path="src/calc.py", text="def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"):
    (repo.path / path).parent.mkdir(parents=True, exist_ok=True)
    (repo.path / path).write_text(text)


def statuses(result):
    return {s["id"]: s["status"] for s in result["steps"]}


def test_create_registers_genesis(tmp_path, chain, alice):
    repo, created = make_repo(tmp_path, chain, alice)
    st = repo_status(repo, chain)
    assert st["registered"] and st["in_sync"] and st["policy_hash_ok"]
    assert st["canonical_head"] == created["genesis_git_oid"]
    assert (repo.path / ".dosr" / "policy.json").exists()


def test_happy_path_approved_and_head_advances(tmp_path, chain, alice, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    edit(repo)
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="Add sub()"))
    result = sub.to_dict()

    assert result["outcome"] == "approved", result["errors"]
    assert set(statuses(result).values()) == {"done"}
    assert all(s["duration_ms"] is not None for s in result["steps"])
    assert result["decision"]["approved"] is True

    new_head = chain.get_head(repo.config["repo_id"])
    assert new_head != created["genesis_git_oid"]
    assert new_head.endswith(repo.git.head())

    # Artifacts are written and the request is what the attestor expects.
    art = repo.load_submission(result["request_id"])
    assert art["request"]["transition"]["parent_git_oid"] == created["genesis_git_oid"]
    assert art["response"]["attestation"]["candidate_git_oid"] == new_head
    assert art["pr"]["stats"]["changed_files"] == 1
    assert "def sub" in art["pr_markdown"]


def test_second_pr_builds_on_new_head(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    assert submit(repo, alice, attestor, chain, SubmitOptions(message="one")).outcome == "approved"
    edit(repo, "src/more.py", "X = 1\n")
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="two"))
    assert sub.outcome == "approved"
    assert sub.to_dict()["info"]["pr"]["changed_files"] == 1


def test_unapproved_model_is_rejected_by_attestor(tmp_path, chain, alice, mallory, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    edit(repo)
    sub = submit(repo, mallory, attestor, chain, SubmitOptions(message="sneaky"))
    result = sub.to_dict()
    assert result["outcome"] == "rejected"
    assert "not approved" in result["decision"]["summary"]
    assert statuses(result)["register"] == "skipped"
    assert chain.get_head(repo.config["repo_id"]) == created["genesis_git_oid"]
    assert any("not in the policy" in w for w in result["warnings"])


def test_strict_policy_rejects_unlisted_attestor(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice, preset="strict")
    edit(repo)
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="change"))
    assert sub.outcome == "rejected"
    assert "local-dev-attestor-1" in sub.decision["summary"]


def test_dirty_tree_without_message_fails_cleanly(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions()).to_dict()
    assert result["outcome"] == "error"
    st = statuses(result)
    assert st["preflight"] == "done" and st["commit"] == "failed" and st["bundle"] == "skipped"
    assert "uncommitted changes" in result["errors"][0]


def test_nothing_to_submit(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    result = submit(repo, alice, attestor, chain, SubmitOptions()).to_dict()
    assert result["outcome"] == "error"
    assert "nothing to submit" in result["errors"][0]


def test_stale_parent_is_caught_before_sending(tmp_path, chain, alice, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    genesis = repo.git.head()
    edit(repo)
    assert submit(repo, alice, attestor, chain, SubmitOptions(message="first")).outcome == "approved"
    # Branch off the old genesis commit and propose a competing change.
    repo.git.run("checkout", "-q", "-b", "stale", genesis)
    edit(repo, "src/other.py", "Y = 2\n")
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="stale")).to_dict()
    assert result["outcome"] == "error"
    assert statuses(result)["parent"] == "failed"
    assert "stale parent" in result["errors"][0]


def test_policy_change_is_refused(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    policy = json.loads((repo.path / ".dosr/policy.json").read_text())
    policy["review"]["max_changed_files"] = 999
    (repo.path / ".dosr/policy.json").write_text(json.dumps(policy))
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="take over")).to_dict()
    assert result["outcome"] == "error"
    # Caught at preflight: the working-tree policy no longer matches the registered hash.
    assert statuses(result)["preflight"] == "failed"


def test_limits_enforced(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice, max_changed_files=1)
    edit(repo, "a.py", "A = 1\n")
    edit(repo, "b.py", "B = 1\n")
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="two files")).to_dict()
    assert result["outcome"] == "error"
    assert statuses(result)["pr"] == "failed"
    assert "max_changed_files" in result["errors"][0]


def test_dry_run_sends_nothing(tmp_path, chain, alice, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="dry", dry_run=True)).to_dict()
    assert result["outcome"] == "dry-run"
    assert statuses(result)["upload"] == "skipped"
    assert chain.get_head(repo.config["repo_id"]) == created["genesis_git_oid"]


def test_attestor_unreachable(tmp_path, chain, alice):
    from dosr_client.attestor import AttestorClient

    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    dead = AttestorClient("http://127.0.0.1:9", timeout=2)
    result = submit(repo, alice, dead, chain, SubmitOptions(message="x")).to_dict()
    assert result["outcome"] == "error"
    assert "cannot reach attestor" in result["errors"][0]


def test_listener_sees_progress(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    seen = []
    submit(repo, alice, attestor, chain, SubmitOptions(message="x"),
           listener=lambda s: seen.append(s.to_dict()["current_step"]))
    assert {"preflight", "bundle", "upload", "await", "register"} <= set(seen)


@pytest.mark.parametrize("mode", ["full", "thin"])
def test_bundle_modes(tmp_path, chain, alice, attestor, mode):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="x", bundle_mode=mode))
    assert sub.outcome == "approved"
    assert sub.info["bundle"]["mode"] == mode
