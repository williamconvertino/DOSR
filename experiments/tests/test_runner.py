import json

import httpx

from dosr_eval.cases import load_cases
from dosr_eval.metrics import load_rows, summarize
from dosr_eval.reviewers import Backend, ModelSpec
from dosr_eval.runner import RunConfig, build_inputs, run
from conftest import CASES

KEY = "sk-test-not-a-real-key"


def _backend(handler) -> Backend:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    b = Backend(ModelSpec.parse("zhipu/glm-4.6@api-v4"), http=http)
    b.key = KEY
    return b


def _reply(content: str, usage=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Same request the attestor sends.
        assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 50}})
    return handler


def _cfg(tmp_path, ids, reps=1, pricing=None):
    return RunConfig(cases=load_cases(CASES, ids), templates=["security-sensitive-review-v2"],
                     models=[ModelSpec.parse("zhipu/glm-4.6@api-v4")], reps=reps, out_dir=tmp_path / "out",
                     work_dir=tmp_path / "w", cache_dir=tmp_path / "g", pricing=pricing or {})


def test_rows_capture_decision_usage_and_cost(tmp_path):
    reject = json.dumps({"approved": False, "summary": "eval() is unsafe", "issues": ["eval"]})
    cfg = _cfg(tmp_path, ["demo-calc-evaluate-injection"], reps=2,
               pricing={"zhipu/glm-4.6@api-v4": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}})
    path = run(cfg, backends={"zhipu/glm-4.6@api-v4": _backend(_reply(reject))}, log=lambda *_: None)

    rows = load_rows(path)
    assert len(rows) == 2
    r = rows[0]
    assert r["approved"] is False and r["expected"] == "reject" and r["parse_error"] is None
    assert r["usage"] == {"prompt_tokens": 1000, "completion_tokens": 50}
    assert abs(r["cost"] - (1000 * 2.0 + 50 * 8.0) / 1e6) < 1e-12
    assert KEY not in path.read_text() and KEY not in (path.with_suffix("").with_suffix(".meta.json")).read_text()
    assert summarize(rows)["accuracy"] == 1.0


def test_unparseable_output_is_a_rejection(tmp_path):
    cfg = _cfg(tmp_path, ["demo-calc-add-power"])
    path = run(cfg, backends={"zhipu/glm-4.6@api-v4": _backend(_reply('{"approved": true'))},
               log=lambda *_: None)
    (r,) = load_rows(path)
    assert r["approved"] is False and r["parse_error"]
    s = summarize([r])
    assert s["false_reject"] == 1.0 and s["parse_fail"] == 1.0


def test_provider_errors_are_recorded_not_scored(tmp_path):
    cfg = _cfg(tmp_path, ["demo-calc-add-power"])
    backend = _backend(lambda req: httpx.Response(429, text="rate limited"))
    (r,) = load_rows(run(cfg, backends={"zhipu/glm-4.6@api-v4": backend}, log=lambda *_: None))
    assert "429" in r["error"]
    assert summarize([r])["errors"] == 1 and summarize([r])["accuracy"] is None


def test_mock_backend_runs_without_key(tmp_path):
    cfg = _cfg(tmp_path, ["demo-calc-add-power", "files-users-sql-injection"])
    cfg.models = [ModelSpec.parse("mock/deterministic-reviewer-v1@1")]
    inputs = build_inputs(cfg)
    rows = load_rows(run(cfg, inputs=inputs, log=lambda *_: None))
    assert [r["approved"] for r in rows] == [True, True]  # the mock approves everything
    assert summarize(rows)["miss"] == 1.0
