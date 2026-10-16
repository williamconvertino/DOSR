"""Repository policy (`.dosr/policy.json`) presets and light validation.

The attestor performs the authoritative, strict schema validation
(services/attestor/attestor/models.py). The checks here only exist so the
client can fail fast with a readable message before contacting the attestor.
"""

import copy
import json
from importlib import resources

from .models import canonical_provider
from .pr_format import PRFormatError, get_template
from .versions import VersionError, validate_spec

PRESETS = {
    "standard": "Single mock reviewer, single local attestor (mirrors examples/repos/hello-python)",
    "strict": "Two pinned models, two attestors, tighter limits (mirrors examples/repos/strict-demo)",
    "llm-review": "Real LLM review: GLM-4-Plus or GLM-4.6 (Zhipu), security + correctness prompt, local attestor",
}

_TOP_LEVEL_KEYS = {"protocol_version", "policy_id", "git", "review", "attestors", "storage"}
_REVIEW_KEYS = {
    "approved_models",
    "minimum_approvals",
    "prompt_template",
    "decision_schema",
    "max_changed_files",
    "max_patch_bytes",
}
# Optional review keys. `context_files`: extra repository paths/globs the PR (and so the
# prompt) must include as context, on top of the template's own context globs.
# `template`: a custom PR template (templates.py), embedded so the policy hash covers it.
_OPTIONAL_REVIEW_KEYS = {"context_files", "template"}
MAX_CONTEXT_GLOBS = 50


class PolicyError(ValueError):
    pass


def load_preset(name: str) -> dict:
    if name not in PRESETS:
        raise PolicyError(f"unknown policy preset {name!r}; choose from {sorted(PRESETS)}")
    text = resources.files("dosr_protocol").joinpath(f"presets/{name}.json").read_text("utf-8")
    return json.loads(text)


def build_policy(
    preset: str = "standard",
    *,
    repo_name: str | None = None,
    approved_models: list[dict] | None = None,
    attestors: list[dict] | None = None,
    prompt_template: str | None = None,
    template: dict | None = None,
    max_changed_files: int | None = None,
    max_patch_bytes: int | None = None,
    minimum_approvals: int | None = None,
    context_files: list[str] | None = None,
) -> dict:
    """Start from a preset and apply overrides chosen at repository creation.
    `template` is a custom PR template definition (a parsed template file): it is
    embedded as `review.template` and named by `review.prompt_template`."""
    policy = copy.deepcopy(load_preset(preset))
    if repo_name:
        policy["policy_id"] = f"dosr-policy:{repo_name}:v1"
    review = policy["review"]
    if approved_models:
        review["approved_models"] = approved_models
    if prompt_template:
        review["prompt_template"] = prompt_template
    if template is not None:
        review["prompt_template"] = template.get("id")
        review["template"] = template
    if max_changed_files is not None:
        review["max_changed_files"] = max_changed_files
    if max_patch_bytes is not None:
        review["max_patch_bytes"] = max_patch_bytes
    if minimum_approvals is not None:
        review["minimum_approvals"] = minimum_approvals
    if context_files:
        review["context_files"] = list(context_files)
    if attestors:
        policy["attestors"] = attestors
    validate_policy(policy)
    return policy


def validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict):
        raise PolicyError("policy must be a JSON object")
    missing = _TOP_LEVEL_KEYS - policy.keys()
    extra = policy.keys() - _TOP_LEVEL_KEYS
    if missing:
        raise PolicyError(f"policy is missing keys: {sorted(missing)}")
    if extra:
        # The attestor rejects unknown fields, so catch them here first.
        raise PolicyError(f"policy has unknown keys (attestor will reject): {sorted(extra)}")
    if policy["protocol_version"] != "0.1":
        raise PolicyError("policy.protocol_version must be '0.1'")
    if policy["git"].get("object_format") not in ("sha1", "sha256"):
        raise PolicyError("policy.git.object_format must be 'sha1' or 'sha256'")
    review = policy["review"]
    if _REVIEW_KEYS - review.keys():
        raise PolicyError(f"policy.review is missing keys: {sorted(_REVIEW_KEYS - review.keys())}")
    unknown = review.keys() - _REVIEW_KEYS - _OPTIONAL_REVIEW_KEYS
    if unknown:
        raise PolicyError(f"policy.review has unknown keys (attestor will reject): {sorted(unknown)}")
    globs = review.get("context_files", [])
    if not isinstance(globs, list) or len(globs) > MAX_CONTEXT_GLOBS or not all(
        isinstance(g, str) and g.strip() and not g.startswith(("/", "../")) and ".." not in g.split("/")
        for g in globs
    ):
        raise PolicyError(f"policy.review.context_files must be a list of at most {MAX_CONTEXT_GLOBS} "
                          "relative paths/globs inside the repository")
    try:
        get_template(review["prompt_template"], review.get("template"))
    except PRFormatError as e:
        raise PolicyError(str(e))
    if not review["approved_models"]:
        raise PolicyError("policy.review.approved_models must not be empty")
    for m in review["approved_models"]:
        if set(m) != {"provider", "model", "version"}:
            raise PolicyError("each approved model needs exactly provider, model, version")
        try:
            validate_spec(m["version"])
        except VersionError as e:
            raise PolicyError(f"approved model {m['provider']}/{m['model']}: {e}")
    for key in ("max_changed_files", "max_patch_bytes", "minimum_approvals"):
        if not isinstance(review[key], int) or review[key] < 1:
            raise PolicyError(f"policy.review.{key} must be a positive integer")
    if not policy["attestors"]:
        raise PolicyError("policy.attestors must not be empty")
    for a in policy["attestors"]:
        if a.get("scheme") != "eip712-secp256k1":
            raise PolicyError("attestor scheme must be 'eip712-secp256k1'")
    if policy["storage"].get("bundle_format") != "git-bundle-v1":
        raise PolicyError("policy.storage.bundle_format must be 'git-bundle-v1'")


def parse_model_spec(spec: str) -> dict:
    """`provider/model@version` -> approved-model dict. In a policy, `version` may be a
    range such as `^1.7.1` (see versions.py); in a review request it must be exact.
    Provider aliases are normalized (`claude/...` -> `anthropic/...`, see models.py)."""
    provider, sep, rest = spec.partition("/")
    model, sep2, version = rest.rpartition("@")
    if not (sep and sep2 and provider and model and version):
        raise PolicyError(f"model must look like provider/model@version, got {spec!r}")
    return {"provider": canonical_provider(provider), "model": model.strip(), "version": version.strip()}


def format_model(m: dict) -> str:
    return f"{m['provider']}/{m['model']}@{m.get('version') or m.get('model_version')}"
