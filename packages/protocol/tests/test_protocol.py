import json
from pathlib import Path

import pytest

from dosr_protocol.canonical import canonical_json, new_request_id, policy_hash, unqualify_oid
from dosr_protocol.policy import PolicyError, build_policy, load_preset, parse_model_spec, validate_policy
from dosr_protocol.pr_format import TEMPLATES, select_context_files
from dosr_protocol.wire import build_review_request, verify_response

ROOT = Path(__file__).resolve().parents[3]


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [2, {"d": 0, "c": "é"}]}) == canonical_json({"a": [2, {"c": "é", "d": 0}], "b": 1})
    assert canonical_json({"a": 1}) == b'{"a":1}'
    with pytest.raises(TypeError):
        canonical_json({"x": 1.5})


def test_policy_hash_ignores_formatting():
    p = load_preset("standard")
    assert policy_hash(p) == policy_hash(json.loads(json.dumps(p, indent=4)))


def test_presets_match_examples():
    for preset, example in (("standard", "hello-python"), ("strict", "strict-demo")):
        ex = json.loads((ROOT / f"examples/repos/{example}/.dosr/policy.json").read_text())
        assert load_preset(preset) == ex


def test_presets_reference_known_templates():
    for name in ("standard", "strict"):
        assert load_preset(name)["review"]["prompt_template"] in TEMPLATES


def test_build_policy_overrides_and_validation():
    p = build_policy("standard", repo_name="r", max_changed_files=3,
                     approved_models=[parse_model_spec("p/m@v1")])
    assert p["policy_id"] == "dosr-policy:r:v1"
    assert p["review"]["approved_models"] == [{"provider": "p", "model": "m", "version": "v1"}]
    bad = dict(p, extra=1)
    with pytest.raises(PolicyError):
        validate_policy(bad)
    with pytest.raises(PolicyError):
        parse_model_spec("nonsense")


def test_request_id_is_ulid_and_sorted():
    a, b = new_request_id(1000), new_request_id(2000)
    assert len(a) == 26 and a < b


def test_unqualify():
    assert unqualify_oid("sha1:abc") == ("sha1", "abc")
    with pytest.raises(ValueError):
        unqualify_oid("abc")


def test_request_matches_wire_example_shape():
    ex = json.loads((ROOT / "examples/wire/1. attestor-request.json").read_text())
    req = build_review_request(
        request_id=ex["request_id"],
        repo=ex["repository"],
        parent_git_oid=ex["transition"]["parent_git_oid"],
        candidate_git_oid=ex["transition"]["candidate_git_oid"],
        bundle_sha256=ex["transition"]["bundle"]["sha256"],
        ipfs_cid=ex["transition"]["bundle"]["ipfs_cid"],
        model={"provider": "mock", "model": "deterministic-reviewer-v1", "version": "1"},
        client_version="0.1.0",
    )
    assert req == ex


def test_verify_response_against_example():
    req = json.loads((ROOT / "examples/wire/1. attestor-request.json").read_text())
    resp = json.loads((ROOT / "examples/wire/3. attestor-response-approved.json").read_text())
    policy = load_preset("standard")
    errors, _ = verify_response(req, resp, policy, now=resp["attestation"]["issued_at"])
    assert errors == []
    resp["attestation"]["candidate_git_oid"] = "sha1:" + "3" * 40
    errors, _ = verify_response(req, resp, policy, now=resp["attestation"]["issued_at"])
    assert any("candidate_git_oid" in e for e in errors)


def test_context_file_selection():
    t = TEMPLATES["security-sensitive-review-v1"]
    picked = select_context_files(t, ["README.md", "src/a.py", "requirements.txt", ".dosr/policy.json"], {"src/a.py"})
    assert picked == ["README.md", "requirements.txt"]
