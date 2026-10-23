"""End-to-end: create repo -> edit -> submit to the real attestor app -> decision."""

import json
from pathlib import Path

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
    st = statuses(result)
    assert st.pop("publish") == "skipped"  # no P2P publisher configured here (see test_p2p.py)
    assert set(st.values()) == {"done"}
    assert all(s["duration_ms"] is not None for s in result["steps"] if s["id"] != "publish")
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
    # The attestor rebuilt the identical PR from the bundle rather than trusting ours.
    assert art["response"]["evidence"]["derived_review_input"]["pr_hash"] == art["pr"]["pr_hash"]


def test_second_pr_builds_on_new_head(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    assert submit(repo, alice, attestor, chain, SubmitOptions(message="one")).outcome == "approved"
    edit(repo, "src/more.py", "X = 1\n")
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="two"))
    assert sub.outcome == "approved"
    assert sub.to_dict()["info"]["pr"]["changed_files"] == 1


def failed_checks(result, key="local_checks"):
    return {c["name"] for c in result["info"].get(key, []) if c["ok"] is False}


def test_unapproved_model_is_blocked_locally(tmp_path, chain, alice, mallory, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, mallory, attestor, chain, SubmitOptions(message="sneaky")).to_dict()
    assert result["outcome"] == "blocked"
    st = statuses(result)
    assert st["validate"] == "failed" and st["upload"] == "skipped"
    assert failed_checks(result) == {"model_is_approved"}
    assert "not approved" in result["errors"][0]
    assert "transfer" not in result["info"]  # nothing was sent
    assert chain.get_head(repo.config["repo_id"]) == created["genesis_git_oid"]


def test_forced_unapproved_model_is_rejected_by_attestor(tmp_path, chain, alice, mallory, attestor):
    repo, created = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, mallory, attestor, chain, SubmitOptions(message="sneaky", force=True)).to_dict()
    assert result["outcome"] == "rejected"
    assert "not approved" in result["decision"]["summary"]
    assert result["decision"]["model_called"] is False
    assert failed_checks(result, "attestor_checks") == {"model_is_approved"}
    assert statuses(result)["register"] == "skipped"
    assert any("FORCED" in w for w in result["warnings"])
    assert chain.get_head(repo.config["repo_id"]) == created["genesis_git_oid"]


def test_strict_policy_unlisted_attestor(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice, preset="strict")
    edit(repo)
    model = {"provider": "provider-a", "model": "review-model-a", "version": "pinned-version"}
    blocked = submit(repo, alice, attestor, chain, SubmitOptions(message="change", model=model))
    assert blocked.outcome == "blocked"
    assert "attestor_is_listed" in failed_checks(blocked.to_dict())
    forced = submit(repo, alice, attestor, chain, SubmitOptions(model=model, force=True))
    assert forced.outcome == "rejected"
    assert "local-dev-attestor-1" in forced.decision["summary"]


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
    assert result["outcome"] == "blocked"
    assert failed_checks(result) == {"within_max_changed_files"}
    assert "max_changed_files" in result["errors"][0]
    # Forced through, the attestor computes the same limit from the bundle.
    forced = submit(repo, alice, attestor, chain, SubmitOptions(force=True)).to_dict()
    assert forced["outcome"] == "rejected"
    assert failed_checks(forced, "attestor_checks") == {"within_max_changed_files"}


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


def test_full_bundle(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    sub = submit(repo, alice, attestor, chain, SubmitOptions(message="x", bundle_mode="full"))
    assert sub.outcome == "approved"
    assert sub.info["bundle"]["mode"] == "full"


def test_thin_bundle_after_attestor_has_parent(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    # The attestor has never seen this repository, so the client falls back to full.
    first = submit(repo, alice, attestor, chain, SubmitOptions(message="one", bundle_mode="thin"))
    assert first.outcome == "approved"
    assert first.info["bundle"]["mode"] == "full"
    assert any("full bundle instead of thin" in w for w in first.warnings)
    # Now it holds the new canonical HEAD, so a thin bundle on top of it works.
    edit(repo, "src/more.py", "X = 1\n")
    second = submit(repo, alice, attestor, chain, SubmitOptions(message="two", bundle_mode="thin"))
    assert second.outcome == "approved", second.errors
    assert second.info["bundle"]["mode"] == "thin"


BAD_MODES = [
    # mode, local check that blocks it, outcome when forced, text in the attestor's answer
    ("unapproved-model", "model_is_approved", "rejected", "not approved"),
    ("outdated-model", "model_is_approved", "rejected", "allowed versions"),
    ("stale-parent", "parent_is_canonical_head", "rejected", "stale parent"),
    ("parent-equals-candidate", "candidate_differs_from_parent", "rejected", "same commit as parent"),
    ("wrong-object-format", "parent_git_oid_format", "rejected", "object format"),
    ("bundle-hash-mismatch", "bundle_hash_matches", "rejected", "bundle hashes to"),
    ("tampered-prompt", "client_prompt_matches", "rejected", "client's prompt"),
    ("wrong-prompt-template", "client_prompt_matches", "rejected", "client's prompt"),
    ("modified-policy", "policy_hash_matches_genesis", "rejected", "registered at genesis"),
    ("malformed-request", "request_is_well_formed", "error", "HTTP 422"),
    ("empty-bundle", "bundle_hash_matches", "error", "HTTP 400"),
]


def land_one_pr(repo, chain, profile, attestor):
    edit(repo, "src/first.py", "FIRST = 1\n")
    assert submit(repo, profile, attestor, chain, SubmitOptions(message="first")).outcome == "approved"


@pytest.mark.parametrize("mode, check, outcome, needle", BAD_MODES)
def test_bad_request_blocked_locally(tmp_path, chain, alice, attestor, mode, check, outcome, needle):
    repo, _ = make_repo(tmp_path, chain, alice)
    if mode == "stale-parent":
        land_one_pr(repo, chain, alice, attestor)
    head = chain.get_head(repo.config["repo_id"])
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="x", bad_request=mode)).to_dict()
    assert result["outcome"] == "blocked", result["errors"]
    assert check in failed_checks(result)
    assert "transfer" not in result["info"]
    assert result["info"]["bad_request"] == mode
    assert chain.get_head(repo.config["repo_id"]) == head


@pytest.mark.parametrize("mode, check, outcome, needle", BAD_MODES)
def test_bad_request_forced_is_rejected_by_attestor(tmp_path, chain, alice, attestor, mode, check, outcome, needle):
    repo, _ = make_repo(tmp_path, chain, alice)
    if mode == "stale-parent":
        land_one_pr(repo, chain, alice, attestor)
    head = chain.get_head(repo.config["repo_id"])
    edit(repo)
    result = submit(repo, alice, attestor, chain,
                    SubmitOptions(message="x", bad_request=mode, force=True)).to_dict()
    assert result["outcome"] == outcome, result["errors"]
    text = (result["decision"] or {}).get("summary", "") + " ".join(result["errors"])
    assert needle in text
    assert any("DEMO" in w for w in result["warnings"])
    assert result["info"]["transfer"]["response_bytes"] > 0
    if outcome == "rejected":
        assert result["decision"]["model_called"] is False  # stopped before any model call
    assert chain.get_head(repo.config["repo_id"]) == head


def test_local_checks_mirror_attestor_checks(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="x")).to_dict()
    local = {c["name"]: c["ok"] for c in result["info"]["local_checks"]}
    remote = {c["name"]: c["ok"] for c in result["info"]["attestor_checks"]}
    server_only = {"provider_is_supported", "model_output_is_valid"}
    assert set(remote) - server_only <= set(local)
    assert all(local.values()) and all(remote.values())
    assert not any("disagree" in w for w in result["warnings"])


def test_prompt_is_sent_and_bound_by_signature(tmp_path, chain, alice, attestor):
    from dosr_protocol.canonical import sha256_0x

    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="x")).to_dict()
    assert result["outcome"] == "approved"
    art = Path(result["artifacts_dir"])
    prompt = (art / "prompt.txt").read_text("utf-8")
    assert prompt == (art / "prompt.derived.txt").read_text("utf-8")
    assert "## Response format" in prompt and "def sub" in prompt
    # The attestor signed the hash of the prompt it sent to the model: ours.
    assert result["decision"]["review_input_hash"] == sha256_0x(prompt.encode()) == result["info"]["prompt"]["sha256"]
    assert result["info"]["transfer"]["prompt_bytes"] == len(prompt.encode())
    assert "model saw exactly our prompt" in next(s for s in result["steps"] if s["id"] == "verify")["detail"]


def test_policy_context_files_reach_the_prompt(tmp_path, chain, alice, attestor):
    seed = tmp_path / "seed"
    (seed / "docs").mkdir(parents=True)
    (seed / "docs" / "STYLE.md").write_text("All functions need docstrings.\n")
    (seed / "notes.txt").write_text("not context\n")
    created = create_repository(
        tmp_path / "repo", name="ctx", policy=build_policy("standard", context_files=["docs/*.md"]),
        profile=alice, chain=chain, seed_dir=seed)
    repo = DosrRepo(created["path"])
    edit(repo, "src/x.py", "X = 1\n")
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="x")).to_dict()
    assert result["outcome"] == "approved", result["errors"]
    assert "docs/STYLE.md" in result["info"]["pr"]["context_files"]
    assert "notes.txt" not in result["info"]["pr"]["context_files"]
    prompt = (Path(result["artifacts_dir"]) / "prompt.txt").read_text("utf-8")
    assert "All functions need docstrings." in prompt
    # The attestor (which reads the same policy) built the same PR from the bundle.
    resp = repo.load_submission(result["request_id"])["response"]
    assert resp["evidence"]["derived_review_input"]["pr_hash"] == result["info"]["pr"]["pr_hash"]


@pytest.mark.parametrize("choice, outcome", [
    (None, "approved"),  # policy default: the lowest version the range allows (1.2.0)
    ({"provider": "mock", "model": "reviewer", "version": "1.9.3"}, "approved"),
    ({"provider": "mock", "model": "reviewer", "version": "1.1.0"}, "blocked"),  # below the minimum
    ({"provider": "mock", "model": "reviewer", "version": "2.0.0"}, "blocked"),  # next major
])
def test_model_minimum_version(tmp_path, chain, alice, attestor, choice, outcome):
    repo, _ = make_repo(tmp_path, chain, alice,
                        approved_models=[{"provider": "mock", "model": "reviewer", "version": "^1.2.0"}])
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="x", model=choice)).to_dict()
    assert result["outcome"] == outcome, result["errors"]
    if outcome == "approved":
        expected = choice["version"] if choice else "1.2.0"
        assert result["decision"]["model"] == f"mock/reviewer@{expected}"
    else:
        assert failed_checks(result) == {"model_is_approved"}
        forced = submit(repo, alice, attestor, chain, SubmitOptions(model=choice, force=True)).to_dict()
        assert forced["outcome"] == "rejected"
        assert "allowed versions: '^1.2.0'" in forced["decision"]["summary"]


def test_sizes_and_word_counts_recorded(tmp_path, chain, alice, attestor):
    repo, _ = make_repo(tmp_path, chain, alice)
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="Add sub()")).to_dict()
    t = result["info"]["transfer"]
    art_dir = Path(repo.load_submission(result["request_id"])["dir"])
    assert t["bundle_bytes"] == (art_dir / "change.bundle").stat().st_size
    assert t["upload_total_bytes"] > t["request_bytes"] + t["policy_bytes"] + t["bundle_bytes"]  # + multipart framing
    assert t["response_bytes"] > 100
    m = result["info"]["pr_metrics"]
    pr_md = repo.load_submission(result["request_id"])["pr_markdown"]
    assert m["total"]["words"] == len(pr_md.split())
    assert m["total"]["est_tokens"] == round(len(pr_md) / 4)
    assert m["sections"]["diff"]["words"] > 0


def test_provider_key_env_names():
    from dosr_client.config import provider_key_env

    assert provider_key_env("mock") is None
    assert provider_key_env("zhipu") == "ZHIPU_API_KEY"
    assert provider_key_env("deep-seek") == "DEEP_SEEK_API_KEY"


def test_model_choice_parsing_and_resolution(monkeypatch):
    from dosr_protocol.policy import build_policy

    from dosr_client.config import provider_api_key, provider_key_env
    from dosr_client.pipeline import approved_model_spec, parse_model_choice, resolve_model

    policy = build_policy("standard", approved_models=[
        {"provider": "openai", "model": "gpt-4o", "version": ">=2024-08-06"},
        {"provider": "mock", "model": "reviewer", "version": "^1.2.0"},
    ])
    assert resolve_model(policy, None) == {"provider": "openai", "model": "gpt-4o", "version": "2024-08-06"}
    assert resolve_model(policy, parse_model_choice("openai/gpt-4o@2024-11-20"))["version"] == "2024-11-20"
    # No version: the policy's entry, else the catalog's newest snapshot.
    assert resolve_model(policy, parse_model_choice("mock/reviewer"))["version"] == "1.2.0"
    assert resolve_model(policy, parse_model_choice("claude/claude-sonnet-4-5")) == {
        "provider": "anthropic", "model": "claude-sonnet-4-5", "version": "20250929"}
    with pytest.raises(Exception, match="provider/model@version"):
        resolve_model(policy, parse_model_choice("acme/unknown"))
    assert approved_model_spec("google/gemini-2.5-pro") == {"provider": "gemini", "model": "gemini-2.5-pro",
                                                           "version": "stable"}

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert provider_key_env("gemini") == "GEMINI_API_KEY" and provider_api_key("gemini") == "g-key"
    assert provider_key_env("mock") is None and provider_api_key("mock") is None


def test_custom_template_file_end_to_end(tmp_path, chain, alice, attestor):
    from dosr_client.templates import load_template_file, scaffold
    from dosr_protocol.pr_format import TEMPLATES

    # `dosr template new` output, edited: a different layout and instructions.
    path = tmp_path / "team-v1.toml"
    text = scaffold("team-v1", TEMPLATES["standard-code-review-v1"])
    text = text.replace('heading = "Diff"', 'heading = "The patch"').replace("# {title}", "# PR: {title}")
    path.write_text(text, encoding="utf-8")
    t = load_template_file(path)
    repo, _ = make_repo(tmp_path, chain, alice, template=t.definition)
    assert repo.policy["review"]["prompt_template"] == "team-v1"
    edit(repo)
    result = submit(repo, alice, attestor, chain, SubmitOptions(message="Add sub()")).to_dict()
    assert result["outcome"] == "approved", result["errors"]
    sub = repo.load_submission(result["request_id"])
    assert sub["pr_markdown"].startswith("# PR: Add sub()") and "## The patch" in sub["pr_markdown"]
    assert json.loads((Path(sub["dir"]) / "pr.json").read_text())["template_hash"] == t.hash
    # The attestor laid out the prompt with the same embedded template.
    assert not failed_checks(result, "attestor_checks")


def test_template_cli_new_check_and_init(tmp_path, chain, monkeypatch):
    from dosr_client.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOSR_TEMPLATES", str(tmp_path / "tpl"))
    assert main(["template", "new", "team-v1", "--from", "security-sensitive-review-v2", "-o", "tpl/team-v1.toml"]) == 0
    assert main(["template", "check", "tpl/team-v1.toml", "--preview"]) == 0
    assert main(["template", "list"]) == 0
    assert main(["template", "show", "team-v1"]) == 0
    assert main(["template", "new", "team-v1", "-o", "tpl/team-v1.toml"]) == 1  # exists
    (tmp_path / "bad.toml").write_text('format = "dosr-pr-template-v1"\nid = "Bad"\n', encoding="utf-8")
    assert main(["template", "check", "bad.toml"]) == 1
    # init resolves the id from DOSR_TEMPLATES and embeds the template.
    rc = main(["--chain-state", str(chain.state_path), "init", str(tmp_path / "r"), "--template", "team-v1"])
    assert rc == 0
    policy = json.loads((tmp_path / "r" / ".dosr" / "policy.json").read_text())
    assert policy["review"]["prompt_template"] == "team-v1" and policy["review"]["template"]["id"] == "team-v1"
