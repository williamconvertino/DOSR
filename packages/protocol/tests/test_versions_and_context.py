import pytest

from dosr_protocol.policy import PolicyError, build_policy, validate_policy
from dosr_protocol.pr_format import TEMPLATES, template_for_policy
from dosr_protocol.versions import (
    VersionError,
    find_approved_model,
    is_range,
    lowest_version,
    validate_spec,
    version_satisfies,
)


@pytest.mark.parametrize("spec, version, ok", [
    ("1.7.1", "1.7.1", True),
    ("1.7.1", "1.7.2", False),
    ("1.7", "1.7.0", True),
    ("api-v4", "api-v4", True),
    ("api-v4", "api-v5", False),
    ("^1.7.1", "1.7.1", True),
    ("^1.7.1", "1.99.0", True),
    ("^1.7.1", "1.7.0", False),
    ("^1.7.1", "2.0.0", False),
    ("^0.7.1", "0.7.9", True),
    ("^0.7.1", "0.8.0", False),
    ("~1.7.1", "1.7.9", True),
    ("~1.7.1", "1.8.0", False),
    (">=1.7.1", "4.0.0", True),
    (">=1.7.1 <1.9", "1.8.5", True),
    (">=1.7.1 <1.9", "1.9.0", False),
    ("1.x", "1.4.2", True),
    ("1.x", "2.0.0", False),
    ("*", "0.0.1", True),
    ("^1.7.1", "^1.7.1", False),  # a request must name one concrete version
    ("^1.7.1", "api-v4", False),
])
def test_version_satisfies(spec, version, ok):
    assert version_satisfies(spec, version) is ok


def test_lowest_version_and_ranges():
    assert lowest_version("^1.7.1") == "1.7.1"
    assert lowest_version(">=1.2 <2") == "1.2.0"
    assert lowest_version("api-v4") == "api-v4"
    assert is_range("^1.0.0") and is_range("1.x") and not is_range("1.0.0") and not is_range("api-v4")
    with pytest.raises(VersionError):
        validate_spec("^not-a-version")


def test_find_approved_model():
    approved = [{"provider": "mock", "model": "r", "version": "^1.2.0"}]
    assert find_approved_model(approved, "mock", "r", "1.3.0") == approved[0]
    assert find_approved_model(approved, "mock", "r", "1.1.0") is None
    assert find_approved_model(approved, "mock", "other", "1.3.0") is None


def test_policy_accepts_ranges_and_context_files():
    p = build_policy("standard", approved_models=[{"provider": "mock", "model": "r", "version": "^1.2.0"}],
                     context_files=["docs/*.md"])
    assert p["review"]["context_files"] == ["docs/*.md"]
    validate_policy(p)


@pytest.mark.parametrize("globs", [["../outside"], ["/abs"], [""], "docs/*.md", [1]])
def test_policy_rejects_bad_context_files(globs):
    p = build_policy("standard")
    p["review"]["context_files"] = globs
    with pytest.raises(PolicyError):
        validate_policy(p)


def test_policy_rejects_bad_version_range_and_unknown_review_key():
    p = build_policy("standard")
    p["review"]["approved_models"][0]["version"] = "^x.y"
    with pytest.raises(PolicyError):
        validate_policy(p)
    p = build_policy("standard")
    p["review"]["surprise"] = 1
    with pytest.raises(PolicyError):
        validate_policy(p)


def test_template_for_policy_adds_globs_and_keeps_the_id():
    base = TEMPLATES["standard-code-review-v1"]
    t = template_for_policy(base.id, ["docs/*.md", "README*"])
    assert t.id == base.id
    assert t.context_globs == base.context_globs + ("docs/*.md",)
    assert template_for_policy(base.id) is base
