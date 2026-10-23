import re

import pytest

from dosr_protocol.models import (
    CATALOG,
    api_model_id,
    canonical_provider,
    catalog_choices,
    key_envs,
    known_versions,
    snapshot_pattern,
)
from dosr_protocol.policy import build_policy, parse_model_spec, validate_policy
from dosr_protocol.versions import find_approved_model, lowest_version, validate_spec, version_satisfies


@pytest.mark.parametrize("provider, model, version, api_id", [
    ("openai", "gpt-4o", "2024-08-06", "gpt-4o-2024-08-06"),
    ("anthropic", "claude-sonnet-4-5", "20250929", "claude-sonnet-4-5-20250929"),
    ("anthropic", "claude-opus-5-5", "stable", "claude-opus-5-5"),
    ("gemini", "gemini-2.5-pro", "stable", "gemini-2.5-pro"),
    ("gemini", "gemini-2.0-flash", "001", "gemini-2.0-flash-001"),
    ("zhipu", "glm-4.6", "api-v4", "glm-4.6"),
    ("openai", "gpt-4o", "20240806", "gpt-4o"),  # not OpenAI's snapshot form: sent as-is
    ("acme", "big", "2024-01-01", "big"),       # unknown provider: never rewritten
])
def test_api_model_id(provider, model, version, api_id):
    assert api_model_id(model, version, snapshot_pattern(provider)) == api_id


@pytest.mark.parametrize("spec, version, ok", [
    (">=2024-08-06", "2024-11-20", True),
    (">=2024-08-06", "2024-08-06", True),
    (">=2024-08-06", "2024-05-13", False),
    (">=20250929", "20251001", True),
    (">=20250929", "20250514", False),
    ("2024-08-06", "2024-08-06", True),
    (">=2024-08-06", "stable", False),
])
def test_dated_snapshot_ranges(spec, version, ok):
    assert version_satisfies(spec, version) is ok


def test_lowest_dated_snapshot_keeps_its_spelling():
    assert lowest_version(">=2024-08-06") == "2024-08-06"
    assert lowest_version(">=20250929") == "20250929"
    assert lowest_version(">=1.2 <2") == "1.2.0"


def test_catalog_is_well_formed():
    rows = catalog_choices()
    assert {"openai", "anthropic", "gemini"} <= {r["provider"] for r in rows}
    for r in rows:
        validate_spec(r["version"])
        assert parse_model_spec(r["spec"]) == {"provider": r["provider"], "model": r["model"], "version": r["version"]}
        pattern = CATALOG[r["provider"]]["snapshot"]
        # A version is either a snapshot of the provider's form or a label like "stable".
        assert r["api_model"] in (r["model"], f"{r['model']}-{r['version']}")
        if pattern and re.fullmatch(pattern, r["version"]):
            assert r["api_model"] == f"{r['model']}-{r['version']}"
    # A policy approving every catalog model is valid.
    p = build_policy("standard", approved_models=[parse_model_spec(r["spec"]) for r in rows])
    validate_policy(p)
    assert known_versions("openai", "gpt-4o")[0] == "2024-11-20"


def test_aliases_and_key_envs():
    assert canonical_provider("Claude") == "anthropic" and canonical_provider("google") == "gemini"
    assert parse_model_spec("claude/claude-sonnet-4-5@>=20250929") == {
        "provider": "anthropic", "model": "claude-sonnet-4-5", "version": ">=20250929"}
    assert key_envs("openai") == ["OPENAI_API_KEY"]
    assert key_envs("gemini") == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    assert key_envs("mock") == []
    assert key_envs("deep-seek") == ["DEEP_SEEK_API_KEY"]


def test_policy_with_snapshot_range_approves_newer_snapshots():
    approved = [parse_model_spec("openai/gpt-4o@>=2024-08-06")]
    assert find_approved_model(approved, "openai", "gpt-4o", "2024-11-20")
    assert not find_approved_model(approved, "openai", "gpt-4o", "2024-05-13")
