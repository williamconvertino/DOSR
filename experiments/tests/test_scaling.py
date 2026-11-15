import pytest

from dosr_eval import overhead
from dosr_eval.scaling import bucket_of, bucket_table, join, linear_fit, model_summary, projection, spearman
from dosr_eval.spend import cost_of, worst_case_call

HAIKU = {"input_per_mtok": 0.1, "output_per_mtok": 0.5, "long_context": {"above_prompt_tokens": 100_000, "multiplier": 5}}


def test_buckets_and_stats():
    assert bucket_of(1) == "1–10" and bucket_of(11) == "11–50" and bucket_of(5000) == "1001+"
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert linear_fit([0, 1, 2], [1, 3, 5]) == pytest.approx((2.0, 1.0))


def test_long_prompt_tier_applies_to_the_whole_call():
    below = cost_of({"prompt_tokens": 100_000, "completion_tokens": 1000}, HAIKU)
    above = cost_of({"prompt_tokens": 100_001, "completion_tokens": 1000}, HAIKU)
    assert below == pytest.approx((100_000 * 0.1 + 1000 * 0.5) / 1e6)
    assert above == pytest.approx(5 * (100_001 * 0.1 + 1000 * 0.5) / 1e6)
    flat = {"input_per_mtok": 0.1, "output_per_mtok": 0.5}
    assert cost_of({"prompt_tokens": 200_000, "completion_tokens": 0}, flat) == pytest.approx(0.02)
    assert worst_case_call(80_000, HAIKU) > worst_case_call(80_000, flat)  # 120K padded estimate crosses the tier


def _rows():
    rows = []
    for case, lines, tok, out, lat in (("a", 5, 4000, 200, 3000), ("b", 40, 9000, 400, 5000), ("c", 20, 6000, 300, 4000)):
        for rep in range(2):
            rows.append({"case": case, "model": "openai/m@1", "template": "t", "context": "none",
                         "latency_ms": lat + rep, "prompt_est_tokens": tok // 2,
                         "usage": {"prompt_tokens": tok, "completion_tokens": out},
                         "cost": cost_of({"prompt_tokens": tok, "completion_tokens": out}, HAIKU)})
    rows.append({"case": "a", "model": "openai/m@1", "error": "429"})
    return rows


SIZES = {"a": {"lines": 5, "files": 1, "patch_bytes": 300, "changed_file_bytes": 5000},
         "b": {"lines": 40, "files": 2, "patch_bytes": 2000, "changed_file_bytes": 20000},
         "c": {"lines": 20, "files": 1, "patch_bytes": 900, "changed_file_bytes": 9000}}


def test_join_summary_and_projection():
    joined = join(_rows(), SIZES)
    assert len(joined) == 6 and {r["bucket"] for r in joined} == {"1–10", "11–50"}
    table = {b["bucket"]: b for b in bucket_table(joined)}
    assert table["1–10"]["calls"] == 2 and table["11–50"]["cases"] == 2

    (s,) = model_summary(joined)
    assert s["token_calibration"] == pytest.approx(2.0)
    assert s["rho_lines_prompt_tokens"] == pytest.approx(1.0)
    assert s["completion_tokens_median"] == 300

    md, rows = projection([s], {100: 3000, 5000: 60_000}, {"openai/m@1": HAIKU})
    small, big = rows
    assert small["prompt_tokens"] == pytest.approx(6000) and small["measured_range"]
    assert big["prompt_tokens"] == pytest.approx(120_000) and big["long_prompt_rate"] and not big["measured_range"]
    assert big["cost_usd"] == pytest.approx(5 * (120_000 * 0.1 + 300 * 0.5) / 1e6)
    assert "long-prompt rate" in md and "extrapolated" in md


def test_overhead_benchmark_runs_real_pipeline():
    rows = overhead.run(sizes=(10, 300), reps=1, log=lambda *_: None)
    summary = overhead.summarize(rows)
    assert [s["lines"] for s in summary] == [10, 300]
    assert summary[1]["prompt_est_tokens"] > summary[0]["prompt_est_tokens"]
    assert all(s["total_ms"] > 0 for s in summary)
    assert {"bundle", "pr", "await", "verify", "register"} <= {r["step"] for r in rows}
