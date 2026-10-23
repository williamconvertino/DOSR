import json

import httpx

from dosr_eval.cases import load_cases
from dosr_eval.metrics import load_rows
from dosr_eval.reviewers import Backend, ModelSpec
from dosr_eval.runner import RunConfig, build_inputs, estimate_cost, run
from dosr_eval.spend import MAX_OUTPUT_TOKENS, spend_report, summarize_spend, worst_case_call
from conftest import CASES

MODEL = "zhipu/glm-4.6@api-v4"
PRICE = {MODEL: {"input_per_mtok": 0.6, "output_per_mtok": 2.2, "currency": "USD"}}
REJECT = json.dumps({"approved": False, "summary": "no", "issues": []})


def _backend(usage):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": REJECT}}], "usage": usage})

    b = Backend(ModelSpec.parse(MODEL), http=httpx.Client(transport=httpx.MockTransport(handler)))
    b.key = "k"
    return b


def _cfg(tmp_path, reps, budget=None, pricing=PRICE):
    return RunConfig(cases=load_cases(CASES, ["demo-calc-add-power"]), templates=["security-sensitive-review-v2"],
                     models=[ModelSpec.parse(MODEL)], reps=reps, out_dir=tmp_path / "out", work_dir=tmp_path / "w",
                     cache_dir=tmp_path / "g", pricing=pricing, budget=budget)


def test_worst_case_bounds_a_real_call():
    price = PRICE[MODEL]
    est = 3000
    # A call that used the estimated prompt and the whole output allowance stays under the bound.
    actual = (est * price["input_per_mtok"] + MAX_OUTPUT_TOKENS * price["output_per_mtok"]) / 1e6
    assert worst_case_call(est, price) >= actual


def test_budget_stops_before_overspending(tmp_path):
    cfg = _cfg(tmp_path, reps=10)
    inputs = build_inputs(cfg)
    per_call_max = worst_case_call(next(iter(inputs.values())).metrics["total"]["est_tokens"], PRICE[MODEL])
    cfg.budget = per_call_max * 3.5  # room for exactly 3 worst-case calls
    usage = {"prompt_tokens": 5000, "completion_tokens": 1024}  # worse than the estimate on input
    path = run(cfg, inputs=inputs, backends={MODEL: _backend(usage)}, log=lambda *_: None)

    rows = load_rows(path)
    spent = sum(r["cost"] for r in rows)
    assert 0 < len(rows) < 10 and spent <= cfg.budget
    meta = json.loads(path.with_name(path.name.replace(".jsonl", ".meta.json")).read_text())
    assert meta["stopped"].startswith("budget") and meta["calls_made"] == len(rows)


def test_budget_needs_a_price(tmp_path):
    cfg = _cfg(tmp_path, reps=1, budget=1.0, pricing={})
    try:
        run(cfg, backends={MODEL: _backend({})}, log=lambda *_: None)
    except SystemExit as e:
        assert "pricing" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_estimate_and_spend_summary(tmp_path):
    cfg = _cfg(tmp_path, reps=2)
    inputs = build_inputs(cfg)
    assert estimate_cost(cfg, inputs) > 0
    assert estimate_cost(_cfg(tmp_path, reps=2, pricing={}), inputs) is None

    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    run(cfg, inputs=inputs, backends={MODEL: _backend(usage)}, log=lambda *_: None)
    (entry,) = summarize_spend(cfg.out_dir, PRICE)
    assert entry["calls"] == 2 and entry["prompt_tokens"] == 2000
    assert abs(entry["cost"] - 2 * (1000 * 0.6 + 100 * 2.2) / 1e6) < 1e-12
    assert "USD" in spend_report([entry], PRICE)
    assert summarize_spend(cfg.out_dir, PRICE, since="2999-01-01") == []


def test_spend_prices_rows_recorded_without_pricing(tmp_path):
    cfg = _cfg(tmp_path, reps=1, pricing={})
    run(cfg, backends={MODEL: _backend({"prompt_tokens": 1_000_000, "completion_tokens": 0})}, log=lambda *_: None)
    assert summarize_spend(cfg.out_dir, {})[0]["unpriced_calls"] == 1
    assert abs(summarize_spend(cfg.out_dir, PRICE)[0]["cost"] - 0.6) < 1e-12
