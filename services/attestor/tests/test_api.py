import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from attestor.main import app

ROOT = Path(__file__).resolve().parents[3]
REQUEST = json.loads((ROOT / "examples/wire/1. attestor-request.json").read_text())
POLICY = json.loads((ROOT / "examples/repos/hello-python/.dosr/policy.json").read_text())
STRICT_POLICY = json.loads((ROOT / "examples/repos/strict-demo/.dosr/policy.json").read_text())

client = TestClient(app)


def post(request=REQUEST, policy=POLICY, bundle=b"fake bundle bytes"):
    to_bytes = lambda x: x if isinstance(x, bytes) else json.dumps(x).encode()
    return client.post(
        "/v1/reviews",
        files={
            "request": ("request.json", to_bytes(request), "application/json"),
            "policy": ("policy.json", to_bytes(policy), "application/json"),
            "bundle": ("change.bundle", bundle, "application/octet-stream"),
        },
    )


def test_root_redirects_to_docs():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/docs"


def test_health():
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_keys():
    r = client.get("/v1/keys")
    assert r.status_code == 200
    assert r.json()["keys"][0]["key_id"] == "local-dev-attestor-1"


def test_example_request_is_approved():
    r = post()
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["approved"] is True
    assert body["request_id"] == REQUEST["request_id"]
    att = body["attestation"]
    assert att["approved"] is True
    assert att["parent_git_oid"] == REQUEST["transition"]["parent_git_oid"]
    assert att["candidate_git_oid"] == REQUEST["transition"]["candidate_git_oid"]
    assert att["expires_at"] > att["issued_at"]
    # Response has the same top-level shape as the wire example.
    example = json.loads((ROOT / "examples/wire/3. attestor-response-approved.json").read_text())
    assert body.keys() == example.keys()
    assert att.keys() == example["attestation"].keys()


def test_unapproved_model_is_rejected():
    req = copy.deepcopy(REQUEST)
    req["review"]["model"] = "some-other-model"
    body = post(request=req).json()
    assert body["result"]["approved"] is False
    assert "not approved" in body["result"]["summary"]


def test_attestor_not_in_policy_is_rejected():
    req = copy.deepcopy(REQUEST)
    req["review"] = {"provider": "provider-a", "model": "review-model-a", "model_version": "pinned-version"}
    body = post(request=req, policy=STRICT_POLICY).json()
    assert body["result"]["approved"] is False
    assert "local-dev-attestor-1" in body["result"]["summary"]


def test_parent_equals_candidate_is_rejected():
    req = copy.deepcopy(REQUEST)
    req["transition"]["candidate_git_oid"] = req["transition"]["parent_git_oid"]
    assert post(request=req).json()["result"]["approved"] is False


def test_invalid_json_is_400():
    assert post(request=b"{not json").status_code == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("transition"),
        lambda r: r.__setitem__("protocol_version", "9.9"),
        lambda r: r["transition"].__setitem__("parent_git_oid", "not-an-oid"),
        lambda r: r.__setitem__("unexpected_field", 1),
    ],
)
def test_malformed_request_is_422(mutate):
    req = copy.deepcopy(REQUEST)
    mutate(req)
    r = post(request=req)
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "request"


def test_malformed_policy_is_422():
    pol = copy.deepcopy(POLICY)
    pol["review"]["approved_models"] = []
    r = post(policy=pol)
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "policy"


def test_empty_bundle_is_400():
    assert post(bundle=b"").status_code == 400


def test_missing_part_is_422():
    r = client.post("/v1/reviews", files={"request": ("r.json", json.dumps(REQUEST).encode())})
    assert r.status_code == 422
