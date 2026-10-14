import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from attestor.config import Settings
from attestor.main import app, get_attestor
from attestor.review import Attestor
from attestor.reviewers import BUILTIN_PROVIDERS, ChatCompletionsReviewer, Provider, load_providers
from dosr_chain import MockChain
from dosr_protocol.canonical import policy_hash
from dosr_protocol.eip712 import recover_signer
from dosr_protocol.policy import load_preset
from dosr_protocol.wire import build_review_request

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_REQUEST = json.loads((ROOT / "examples/wire/1. attestor-request.json").read_text())
STANDARD = load_preset("standard")
STRICT = load_preset("strict")
REPO_ID = "0x" + "ab" * 32
DEV_ADDRESS = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


class Repo:
    """A DOSR repository on disk, registered on a mock chain."""

    def __init__(self, path: Path, chain: MockChain, policy: dict):
        self.path, self.chain, self.policy = path, chain, policy
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write(".dosr/policy.json", json.dumps(policy, indent=2))
        self.write("src/app.py", "def f():\n    return 1\n")
        self.genesis = self.commit("genesis")
        chain.register_repository(repo_id=REPO_ID, name="r", policy=policy,
                                  genesis_git_oid=self.q(self.genesis), object_format="sha1")

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.path, env=GIT_ENV, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text):
        (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.path / rel).write_text(text)

    def commit(self, msg, files=None) -> str:
        for rel, text in (files or {}).items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    @staticmethod
    def q(oid):
        return "sha1:" + oid

    def bundle(self, candidate, parent=None) -> bytes:
        out = self.path.parent / f"{candidate}.bundle"
        self.git("update-ref", "refs/dosr/candidate", candidate)
        self.git("bundle", "create", "-q", str(out),
                 f"{parent}..refs/dosr/candidate" if parent else "refs/dosr/candidate")
        return out.read_bytes()

    def request(self, parent, candidate, bundle: bytes, model=None) -> dict:
        return build_review_request(
            request_id="01TEST",
            repo={"repo_id": REPO_ID, "chain_id": self.chain.chain_id, "contract": self.chain.contract,
                  "policy_hash": policy_hash(self.policy)},
            parent_git_oid=self.q(parent),
            candidate_git_oid=self.q(candidate),
            bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
            model=model or {"provider": "mock", "model": "deterministic-reviewer-v1", "version": "1"},
            client_version="test",
        )


@pytest.fixture
def chain(tmp_path):
    return MockChain(tmp_path / "chain.json")


@pytest.fixture
def client(tmp_path, chain):
    settings = Settings(chain_state=chain.state_path, cache_dir=tmp_path / "cache")
    attestor = Attestor(settings)
    app.dependency_overrides[get_attestor] = lambda: attestor
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def repo(tmp_path, chain):
    return Repo(tmp_path / "repo", chain, STANDARD)


def post(client, request, policy, bundle):
    to_bytes = lambda x: x if isinstance(x, bytes) else json.dumps(x).encode()
    return client.post(
        "/v1/reviews",
        files={
            "request": ("request.json", to_bytes(request), "application/json"),
            "policy": ("policy.json", to_bytes(policy), "application/json"),
            "bundle": ("change.bundle", bundle, "application/octet-stream"),
        },
    )


def review(client, repo, parent, candidate, bundle=None, request=None, policy=None):
    bundle = repo.bundle(candidate) if bundle is None else bundle
    request = request or repo.request(parent, candidate, bundle)
    r = post(client, request, policy or repo.policy, bundle)
    assert r.status_code == 200, r.text
    return r.json()


def failed(body) -> dict:
    return {c["name"]: c["detail"] for c in body["evidence"]["checks"] if not c["ok"]}


# ---------- basics ----------


def test_root_redirects_to_docs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/docs"


def test_health(client):
    assert client.get("/v1/health").json()["status"] == "ok"


def test_keys_report_the_signing_address(client):
    key = client.get("/v1/keys").json()["keys"][0]
    assert key == {"key_id": "local-dev-attestor-1", "scheme": "eip712-secp256k1", "address": DEV_ADDRESS}


# ---------- happy path ----------


def test_approved_transition_is_signed_and_accepted_by_chain(client, repo, chain):
    cand = repo.commit("change", {"src/app.py": "def f():\n    return 2\n"})
    body = review(client, repo, repo.genesis, cand)

    assert body["result"]["approved"] is True, body["result"]["summary"]
    assert failed(body) == {}
    derived = body["evidence"]["derived_review_input"]
    assert derived["changed_files"] == ["src/app.py"] and derived["commit_count"] == 1
    assert recover_signer(body["attestation"], body["signature"]["value"]) == DEV_ADDRESS

    # Same top-level shape as the wire example.
    example = json.loads((ROOT / "examples/wire/3. attestor-response-approved.json").read_text())
    assert body.keys() == example.keys()
    assert body["attestation"].keys() == example["attestation"].keys()

    chain.accept_transition(body)
    assert chain.get_head(REPO_ID) == repo.q(cand)


def test_candidate_several_commits_ahead_is_reviewed_as_one_diff(client, repo):
    repo.commit("one", {"src/a.py": "A = 1\n"})
    cand = repo.commit("two", {"src/b.py": "B = 1\n"})
    body = review(client, repo, repo.genesis, cand)
    assert body["result"]["approved"] is True
    assert body["evidence"]["derived_review_input"]["commit_count"] == 2
    assert sorted(body["evidence"]["derived_review_input"]["changed_files"]) == ["src/a.py", "src/b.py"]


def test_request_hash_is_over_canonical_json(client, repo):
    cand = repo.commit("change", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    a = post(client, json.dumps(req).encode(), STANDARD, bundle).json()
    b = post(client, json.dumps(req, indent=4, sort_keys=True).encode(), STANDARD, bundle).json()
    assert a["attestation"]["request_hash"] == b["attestation"]["request_hash"]


class FixedReviewer:
    """Returns a canned raw model output and remembers the prompt it was given."""

    def __init__(self, raw):
        self.raw, self.prompts = raw, []

    def complete(self, prompt, model, api_key=None):
        self.prompts.append(prompt)
        return self.raw


def use_reviewer(chain, tmp_path, reviewer, provider="mock"):
    attestor = Attestor(Settings(chain_state=chain.state_path, cache_dir=tmp_path / "cache2"),
                        reviewers={provider: reviewer})
    app.dependency_overrides[get_attestor] = lambda: attestor


def test_signed_hashes_cover_prompt_and_raw_model_output(client, repo, chain, tmp_path):
    raw = '```json\n{"approved": false, "summary": "Breaks f().", "issues": ["f returns 2"]}\n```'
    reviewer = FixedReviewer(raw)
    use_reviewer(chain, tmp_path, reviewer)
    cand = repo.commit("change", {"src/app.py": "def f():\n    return 2\n"})
    body = review(client, repo, repo.genesis, cand)

    assert body["result"] == {"approved": False, "summary": "Breaks f().", "issues": ["f returns 2"]}
    [prompt] = reviewer.prompts
    # The prompt was built by the attestor from the bundle, and its hash is signed.
    assert "-    return 1\n+    return 2" in prompt and "## Response format" in prompt
    att = body["attestation"]
    assert att["review_input_hash"] == "0x" + hashlib.sha256(prompt.encode()).hexdigest()
    # response_hash is over the raw model text, before parsing.
    assert att["response_hash"] == "0x" + hashlib.sha256(raw.encode()).hexdigest()
    assert body["evidence"]["model_response_hash"] == att["response_hash"]
    assert recover_signer(att, body["signature"]["value"]) == DEV_ADDRESS


def test_unparseable_model_output_is_rejected(client, repo, chain, tmp_path):
    use_reviewer(chain, tmp_path, FixedReviewer("Looks good to me, approved!"))
    cand = repo.commit("change", {"src/x.py": "X = 1\n"})
    body = review(client, repo, repo.genesis, cand)
    assert body["result"]["approved"] is False
    assert "model_output_is_valid" in failed(body)


def test_model_is_not_called_when_checks_fail(client, repo, chain, tmp_path):
    reviewer = FixedReviewer('{"approved": true, "summary": "ok", "issues": []}')
    use_reviewer(chain, tmp_path, reviewer)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle, model={"provider": "mock", "model": "always-approve", "version": "1"})
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert reviewer.prompts == []
    assert body["attestation"]["review_input_hash"] == body["attestation"]["response_hash"] == "0x" + "00" * 32


# ---------- bundle ----------


def test_bundle_hash_mismatch(client, repo):
    cand = repo.commit("change", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    req["transition"]["bundle"]["sha256"] = "sha256:" + "0" * 64
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert body["result"]["approved"] is False
    assert "bundle_hash_matches" in failed(body)


def test_bundle_without_candidate(client, repo):
    other = repo.commit("other", {"src/x.py": "X = 1\n"})
    cand = repo.commit("cand", {"src/y.py": "Y = 1\n"})
    body = review(client, repo, repo.genesis, cand, bundle=repo.bundle(other))
    assert "does not contain the candidate" in failed(body)["bundle_contains_transition"]


def test_garbage_bundle(client, repo):
    cand = repo.commit("cand", {"src/y.py": "Y = 1\n"})
    body = review(client, repo, repo.genesis, cand, bundle=b"definitely not a bundle")
    assert "not a Git bundle" in failed(body)["bundle_contains_transition"]


def test_candidate_not_descended_from_parent(client, repo, chain):
    a = repo.commit("a", {"src/a.py": "A = 1\n"})
    chain.accept_transition(review(client, repo, repo.genesis, a))
    # A sibling of `a` (also built on genesis) claimed to sit on top of `a`.
    repo.git("checkout", "-q", "-b", "side", repo.genesis)
    b = repo.commit("b", {"src/b.py": "B = 1\n"})
    repo.git("update-ref", "refs/heads/keep-a", a)
    full = repo.path.parent / "both.bundle"
    repo.git("update-ref", "refs/dosr/candidate", b)
    repo.git("bundle", "create", "-q", str(full), "refs/dosr/candidate", "refs/heads/keep-a")
    body = review(client, repo, a, b, bundle=full.read_bytes())
    assert "does not descend" in failed(body)["bundle_contains_transition"]


def test_thin_bundle_needs_cached_parent(client, repo, chain):
    a = repo.commit("a", {"src/app.py": "def f():\n    return 2\n"})
    thin = repo.bundle(a, parent=repo.genesis)
    r = post(client, repo.request(repo.genesis, a, thin), STANDARD, thin)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "missing_prerequisites"
    assert client.get(f"/v1/repos/{REPO_ID}/commits/{repo.q(repo.genesis)}").json() == {"present": False}

    # A full bundle teaches the attestor these objects...
    chain.accept_transition(review(client, repo, repo.genesis, a))
    assert client.get(f"/v1/repos/{REPO_ID}/commits/{repo.q(a)}").json() == {"present": True}
    # ...so the next change can be sent thin.
    b = repo.commit("b", {"src/app.py": "def f():\n    return 3\n"})
    body = review(client, repo, a, b, bundle=repo.bundle(b, parent=a))
    assert body["result"]["approved"] is True, body["result"]["summary"]


# ---------- chain state ----------


def test_stale_parent(client, repo, chain):
    a = repo.commit("a", {"src/a.py": "A = 1\n"})
    chain.accept_transition(review(client, repo, repo.genesis, a))
    b = repo.commit("b", {"src/b.py": "B = 1\n"})
    body = review(client, repo, repo.genesis, b)  # still claims genesis as parent
    assert "stale parent" in failed(body)["parent_is_canonical_head"]
    assert body["evidence"]["derived_review_input"] is None  # bundle never opened


def test_unregistered_repository(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    req["repository"]["repo_id"] = "0x" + "cd" * 32
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert "repository_exists" in failed(body)


def test_wrong_chain(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    req["repository"]["chain_id"] = 1
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert "chain_matches" in failed(body)


def test_policy_differs_from_genesis(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    loose = copy.deepcopy(STANDARD)
    loose["review"]["max_changed_files"] = 999
    req = repo.request(repo.genesis, cand, bundle)
    req["repository"]["policy_hash"] = policy_hash(loose)  # self-consistent, but not the registered policy
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req, policy=loose)
    assert set(failed(body)) == {"policy_hash_matches_genesis"}


# ---------- request vs policy ----------


def test_policy_hash_mismatch(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    req["repository"]["policy_hash"] = "0x" + "0" * 64
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert "policy_hash_matches" in failed(body)


def test_unapproved_model(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle, model={"provider": "mock", "model": "always-approve", "version": "1"})
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert body["result"]["approved"] is False
    assert "not approved" in body["result"]["summary"]
    # A rejection is still signed, so the client can show it was really the attestor.
    assert recover_signer(body["attestation"], body["signature"]["value"]) == DEV_ADDRESS


def test_attestor_not_in_policy(client, tmp_path, chain):
    repo = Repo(tmp_path / "strict", chain, STRICT)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    model = STRICT["review"]["approved_models"][0]
    bundle = repo.bundle(cand)
    body = review(client, repo, repo.genesis, cand, bundle=bundle,
                  request=repo.request(repo.genesis, cand, bundle, model=model))
    assert body["result"]["approved"] is False
    assert "local-dev-attestor-1" in body["result"]["summary"]


def test_policy_lists_attestor_with_other_address(client, tmp_path, chain):
    policy = copy.deepcopy(STANDARD)
    policy["attestors"][0]["address"] = "0x" + "12" * 20
    repo = Repo(tmp_path / "r2", chain, policy)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    assert "attestor_address_matches" in failed(review(client, repo, repo.genesis, cand))


def test_parent_equals_candidate(client, repo):
    bundle = repo.bundle(repo.genesis)
    body = review(client, repo, repo.genesis, repo.genesis, bundle=bundle)
    assert "same commit as parent" in body["result"]["summary"]


def test_wrong_object_format(client, repo):
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle)
    req["transition"]["candidate_git_oid"] = "sha256:" + "1" * 64
    body = review(client, repo, repo.genesis, cand, bundle=bundle, request=req)
    assert "object format" in body["result"]["summary"]


# ---------- what the change contains ----------


def test_policy_file_change_is_rejected(client, repo):
    loose = copy.deepcopy(STANDARD)
    loose["review"]["max_changed_files"] = 999
    cand = repo.commit("take over", {".dosr/policy.json": json.dumps(loose)})
    f = failed(review(client, repo, repo.genesis, cand))
    assert ".dosr/policy.json" in f["protected_paths_unchanged"]
    assert "policy_file_matches" in f


def test_limits(client, tmp_path, chain):
    policy = copy.deepcopy(STANDARD)
    policy["review"]["max_changed_files"] = 1
    policy["review"]["max_patch_bytes"] = 200
    repo = Repo(tmp_path / "small", chain, policy)
    cand = repo.commit("big", {"src/a.py": "A = 1\n" * 50, "src/b.py": "B = 1\n"})
    f = failed(review(client, repo, repo.genesis, cand))
    assert "max_changed_files=1" in f["within_max_changed_files"]
    assert "max_patch_bytes=200" in f["within_max_patch_bytes"]


# ---------- malformed requests ----------


def test_invalid_json_is_400(client):
    assert post(client, b"{not json", STANDARD, b"x").status_code == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("transition"),
        lambda r: r.__setitem__("protocol_version", "9.9"),
        lambda r: r["transition"].__setitem__("parent_git_oid", "not-an-oid"),
        lambda r: r.__setitem__("unexpected_field", 1),
    ],
)
def test_malformed_request_is_422(client, mutate):
    req = copy.deepcopy(EXAMPLE_REQUEST)
    mutate(req)
    r = post(client, req, STANDARD, b"x")
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "request"


def test_malformed_policy_is_422(client):
    pol = copy.deepcopy(STANDARD)
    pol["review"]["approved_models"] = []
    r = post(client, EXAMPLE_REQUEST, pol, b"x")
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "policy"


def test_empty_bundle_is_400(client):
    assert post(client, EXAMPLE_REQUEST, STANDARD, b"").status_code == 400


def test_oversized_bundle_is_413(client, chain, tmp_path):
    small = Attestor(Settings(chain_state=chain.state_path, cache_dir=tmp_path / "c", max_bundle_bytes=10))
    app.dependency_overrides[get_attestor] = lambda: small
    assert post(client, EXAMPLE_REQUEST, STANDARD, b"x" * 11).status_code == 413


def test_missing_part_is_422(client):
    r = client.post("/v1/reviews", files={"request": ("r.json", json.dumps(EXAMPLE_REQUEST).encode())})
    assert r.status_code == 422


# ---------- OpenAI-compatible backends (fake transport, no network) ----------

GLM = {"provider": "zhipu", "model": "glm-4.6", "version": "api-v4"}


def glm_repo(tmp_path, chain):
    policy = copy.deepcopy(STANDARD)
    policy["review"]["approved_models"] = [GLM]
    return Repo(tmp_path / "glm", chain, policy)


def fake_zhipu(handler):
    return ChatCompletionsReviewer("zhipu", BUILTIN_PROVIDERS["zhipu"],
                                   http=httpx.Client(transport=httpx.MockTransport(handler)))


def post_with_key(client, repo, cand, key):
    bundle = repo.bundle(cand)
    req = repo.request(repo.genesis, cand, bundle, model=GLM)
    files = {
        "request": ("request.json", json.dumps(req).encode(), "application/json"),
        "policy": ("policy.json", json.dumps(repo.policy).encode(), "application/json"),
        "bundle": ("change.bundle", bundle, "application/octet-stream"),
    }
    return client.post("/v1/reviews", files=files, headers={"X-DOSR-Provider-Key": key} if key else {})


def test_zhipu_call_uses_contributor_key_and_fixed_parameters(client, chain, tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        content = '{"approved": true, "summary": "Fine.", "issues": []}'
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    use_reviewer(chain, tmp_path, fake_zhipu(handler), provider="zhipu")
    repo = glm_repo(tmp_path, chain)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    r = post_with_key(client, repo, cand, "secret-key-123")

    assert r.status_code == 200 and r.json()["result"]["approved"] is True
    [req] = seen
    assert req.headers["authorization"] == "Bearer secret-key-123"
    body = json.loads(req.content)
    assert body["model"] == "glm-4.6" and body["temperature"] == 0
    assert body["thinking"] == {"type": "disabled"} and body["response_format"] == {"type": "json_object"}
    # The signed prompt hash is over exactly what was sent.
    sent = body["messages"][0]["content"]
    assert r.json()["attestation"]["review_input_hash"] == "0x" + hashlib.sha256(sent.encode()).hexdigest()
    assert "secret-key-123" not in r.text


def test_zhipu_without_key_is_400(client, chain, tmp_path):
    use_reviewer(chain, tmp_path, fake_zhipu(lambda request: None), provider="zhipu")
    repo = glm_repo(tmp_path, chain)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    r = post_with_key(client, repo, cand, None)
    assert r.status_code == 400 and "X-DOSR-Provider-Key" in r.text


def test_zhipu_upstream_error_is_502_and_unsigned(client, chain, tmp_path):
    use_reviewer(chain, tmp_path, fake_zhipu(lambda request: httpx.Response(401, json={"error": "bad key"})),
                 provider="zhipu")
    repo = glm_repo(tmp_path, chain)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    r = post_with_key(client, repo, cand, "wrong-key")
    assert r.status_code == 502 and "HTTP 401" in r.text
    assert "signature" not in r.text


def test_unsupported_provider_is_rejected_before_any_call(client, chain, tmp_path):
    use_reviewer(chain, tmp_path, FixedReviewer("unused"))  # only "mock" is available
    repo = glm_repo(tmp_path, chain)
    cand = repo.commit("c", {"src/x.py": "X = 1\n"})
    r = post_with_key(client, repo, cand, "k")
    assert "provider_is_supported" in failed(r.json())


def test_provider_added_by_config_file(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"acme": {"url": "https://llm.acme.test/v1/chat/completions",
                                         "extra_body": {"seed": 7}}}))
    providers = load_providers(path)
    assert providers["acme"] == Provider(url="https://llm.acme.test/v1/chat/completions", extra_body={"seed": 7})
    assert "zhipu" in providers  # built-ins stay

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    reviewer = ChatCompletionsReviewer("acme", providers["acme"], http=httpx.Client(transport=httpx.MockTransport(handler)))
    model = type("M", (), {"model": "acme-large"})()
    assert reviewer.complete("prompt", model, "k") == "{}"
    assert str(seen[0].url) == "https://llm.acme.test/v1/chat/completions"
    body = json.loads(seen[0].content)
    assert body["seed"] == 7 and "thinking" not in body
