import pytest

from dosr_eval.figures import matrix_data, matrix_tables, rate_data, rate_tables, wilson, write_all


def row(cell, approved, template="t1", model="zhipu/glm-4.6@api-v4", case="c", rep=0, **kw):
    expected = "approve" if cell == "high-good" else "reject"
    return {"case": case, "category": "x", "cell": cell, "expected": expected, "template": template,
            "model": model, "rep": rep, "approved": approved, "parse_error": None, "latency_ms": 1000.0,
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}, "cost": 0.001, **kw}


ROWS = [
    row("high-good", True, rep=0), row("high-good", False, rep=1),
    row("high-bad", True, case="d"), row("low-good", False, case="e"), row("low-bad", False, case="f"),
    row("high-good", True, template="t2"), {**row("high-bad", None, template="t2", case="d"), "error": "500"},
]


def test_wilson_interval():
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi and abs((lo + hi) / 2 - 0.5) < 1e-9
    assert wilson(0, 0) is None
    assert wilson(10, 10)[1] == pytest.approx(1.0)


def test_matrix_counts_and_skips_errors():
    m = matrix_data(ROWS)
    assert m["t1"]["high-good"]["accept"] == 0.5 and m["t1"]["high-good"]["n"] == 2
    assert m["t1"]["high-bad"]["accept"] == 1.0
    assert m["t2"]["high-bad"]["n"] == 0 and m["t2"]["high-bad"]["accept"] is None


def test_rates():
    r = rate_data(ROWS)["t1"]
    assert r["false_reject"]["value"] == 0.5 and r["miss"]["value"] == 1 / 3
    assert r["accuracy"]["value"] == 3 / 5


def test_tables_mark_placeholder_and_required_outcome():
    tex, md = matrix_tables(ROWS)
    assert "\\label{tab:template-matrix}" in tex and "Placeholder" not in tex
    assert "\\textbf{50\\%}" in tex  # accept is the required outcome for high-good
    mock = [{**r, "model": "mock/deterministic-reviewer-v1@1"} for r in ROWS]
    assert "Placeholder data" in matrix_tables(mock)[1] and "Placeholder data" in rate_tables(mock)[0]


def test_two_models_become_separate_series():
    rows = ROWS + [row("high-good", True, model="zhipu/glm-4-plus@api-v4")]
    assert set(matrix_data(rows)) == {"t1 · zhipu/glm-4.6@api-v4", "t2 · zhipu/glm-4.6@api-v4",
                                      "t1 · zhipu/glm-4-plus@api-v4"}


def test_write_all(tmp_path):
    pytest.importorskip("matplotlib")
    paths = write_all(ROWS, tmp_path / "fig", "USD")
    names = {p.name for p in paths}
    assert {"matrix.tex", "rates.md", "cost.tex", "accept_by_cell.pdf", "rates.png"} <= names
    assert all(p.stat().st_size > 0 for p in paths)
