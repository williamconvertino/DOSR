from dosr_eval.metrics import report, summarize


def row(case, expected, approved, rep=0, parse_error=None, **kw):
    return {"case": case, "category": "benign", "expected": expected, "template": "t", "model": "m",
            "rep": rep, "approved": approved, "parse_error": parse_error, "latency_ms": 10.0, **kw}


def test_summary_rates():
    rows = [
        row("a", "approve", True, 0), row("a", "approve", False, 1),   # flips; one false reject
        row("b", "reject", False, 0), row("b", "reject", False, 1),    # stable, correct
        row("c", "reject", True, 0), row("c", "reject", False, 1, parse_error="bad json"),
        {**row("d", "approve", None), "error": "HTTP 500"},
    ]
    s = summarize(rows)
    assert s["calls"] == 7 and s["errors"] == 1
    assert s["accuracy"] == 4 / 6
    assert s["false_reject"] == 1 / 2
    assert s["miss"] == 1 / 4
    assert s["parse_fail"] == 1 / 6
    assert s["flip"] == 2 / 3  # a and c flip, b does not


def test_report_lists_wrong_verdicts():
    text = report([row("a", "approve", False, summary="looks | risky")])
    assert "| a | t | m | 0 | approve | reject | looks \\| risky |" in text
