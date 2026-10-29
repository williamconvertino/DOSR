"""OpenAI and Anthropic go through the attestor's own backends; paid calls need --paid."""

import json

import httpx
import pytest

from dosr_eval.__main__ import main
from dosr_eval.cases import load_cases
from dosr_eval.metrics import load_rows
from dosr_eval.reviewers import Backend, ModelSpec, normalize_usage
from dosr_eval.runner import RunConfig, run
from conftest import CASES

DECISION = json.dumps({"approved": False, "summary": "unsafe", "issues": ["x"]})


def _backend(spec: str, handler, monkeypatch, env: str) -> Backend:
    monkeypatch.setenv(env, "sk-test-not-a-real-key")
    return Backend(ModelSpec.parse(spec), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_openai_uses_the_attestor_request(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.host == "api.openai.com"
        assert body["model"] == "gpt-5-2025-08-07"  # dated version -> snapshot id
        assert "temperature" not in body and body["max_completion_tokens"] == 8192  # reasoning-model extras
        assert request.headers["authorization"] == "Bearer sk-test-not-a-real-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": DECISION}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    b = _backend("openai/gpt-5@2025-08-07", handler, monkeypatch, "OPENAI_API_KEY")
    raw, usage = b.complete("prompt")
    assert raw == DECISION and usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_anthropic_uses_the_messages_api_and_usage_is_normalized(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "sk-test-not-a-real-key"
        assert body["model"] == "claude-test-model" and body["max_tokens"] == 1024  # non-snapshot version ignored
        return httpx.Response(200, json={"content": [{"type": "text", "text": DECISION}],
                                         "usage": {"input_tokens": 100, "output_tokens": 7,
                                                   "cache_read_input_tokens": 20}})

    b = _backend("anthropic/claude-test-model@latest", handler, monkeypatch, "ANTHROPIC_API_KEY")
    raw, usage = b.complete("prompt")
    assert raw == DECISION
    assert usage["prompt_tokens"] == 120 and usage["completion_tokens"] == 7


def test_normalize_usage_leaves_chat_completions_alone():
    assert normalize_usage({"prompt_tokens": 1, "completion_tokens": 2}) == {"prompt_tokens": 1, "completion_tokens": 2}
    assert normalize_usage(None) is None


def test_paid_models_need_the_paid_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("made an HTTP call"))
    code = main(["--cases", str(CASES), "run", "--case", "demo-calc-add-power",
                 "--model", "openai/gpt-5@2025-08-07", "--out", str(tmp_path)])
    assert code == 2 and "--paid" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_budget_stops_before_the_next_call(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": DECISION}}],
                                         "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})

    spec = "openai/gpt-4.1@2025-04-14"
    b = _backend(spec, handler, monkeypatch, "OPENAI_API_KEY")
    cfg = RunConfig(cases=load_cases(CASES, ["demo-calc-add-power"]), templates=["security-sensitive-review-v2"],
                    models=[ModelSpec.parse(spec)], reps=3, out_dir=tmp_path / "out", work_dir=tmp_path / "w",
                    cache_dir=tmp_path / "g", budget=1.5,
                    pricing={spec: {"input_per_mtok": 1.0, "output_per_mtok": 1.0}})
    path = run(cfg, backends={spec: b}, log=lambda *_: None)
    assert len(calls) == 2 and len(load_rows(path)) == 2  # 1.0 spent; then 2.0 + next call > 1.5: stop
    meta = json.loads(path.with_suffix("").with_suffix(".meta.json").read_text())
    assert meta["stopped"].startswith("budget 1.5 reached")


def test_budget_needs_prices(tmp_path, monkeypatch):
    spec = "openai/gpt-4.1@2025-04-14"
    b = _backend(spec, lambda r: pytest.fail("called"), monkeypatch, "OPENAI_API_KEY")
    cfg = RunConfig(cases=load_cases(CASES, ["demo-calc-add-power"]), templates=["security-sensitive-review-v2"],
                    models=[ModelSpec.parse(spec)], out_dir=tmp_path / "out", work_dir=tmp_path / "w",
                    cache_dir=tmp_path / "g", budget=1.0)
    with pytest.raises(SystemExit, match="needs a price"):
        run(cfg, backends={spec: b}, log=lambda *_: None)
