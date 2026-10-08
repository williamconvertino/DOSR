"""Repository policy (`.dosr/policy.json`) presets and light validation.

The attestor performs the authoritative, strict schema validation
(services/attestor/attestor/models.py). The checks here only exist so the
client can fail fast with a readable message before contacting the attestor.
"""

import copy
import json
from importlib import resources

PRESETS = {
    "standard": "Single mock reviewer, single local attestor (mirrors examples/repos/hello-python)",
    "strict": "Two pinned models, two attestors, tighter limits (mirrors examples/repos/strict-demo)",
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
    max_changed_files: int | None = None,
    max_patch_bytes: int | None = None,
    minimum_approvals: int | None = None,
) -> dict:
    """Start from a preset and apply overrides chosen at repository creation."""
    policy = copy.deepcopy(load_preset(preset))
    if repo_name:
        policy["policy_id"] = f"dosr-policy:{repo_name}:v1"
    review = policy["review"]
    if approved_models:
        review["approved_models"] = approved_models
    if prompt_template:
        review["prompt_template"] = prompt_template
    if max_changed_files is not None:
        review["max_changed_files"] = max_changed_files
    if max_patch_bytes is not None:
        review["max_patch_bytes"] = max_patch_bytes
    if minimum_approvals is not None:
        review["minimum_approvals"] = minimum_approvals
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
    if not review["approved_models"]:
        raise PolicyError("policy.review.approved_models must not be empty")
    for m in review["approved_models"]:
        if set(m) != {"provider", "model", "version"}:
            raise PolicyError("each approved model needs exactly provider, model, version")
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
    """`provider/model@version` -> approved-model dict."""
    provider, sep, rest = spec.partition("/")
    model, sep2, version = rest.rpartition("@")
    if not (sep and sep2 and provider and model and version):
        raise PolicyError(f"model must look like provider/model@version, got {spec!r}")
    return {"provider": provider, "model": model, "version": version}


def format_model(m: dict) -> str:
    return f"{m['provider']}/{m['model']}@{m.get('version') or m.get('model_version')}"
