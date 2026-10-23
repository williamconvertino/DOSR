"""Catalog of common review models (OpenAI, Anthropic Claude, Google Gemini, Zhipu GLM).

Policies name models as `provider/model@version`. The catalog only makes the common
ones easy to pick (client CLI/GUI) and tells the attestor how to call them; any other
`provider/model@version` can still be written by hand, and approval is always decided
by the policy, never by this list.

`version` is the model *snapshot*. For providers that publish dated snapshots the
version is appended to the model name to get the exact API model id the attestor calls:

    openai/gpt-4o@2024-08-06               -> gpt-4o-2024-08-06
    anthropic/claude-sonnet-4-5@20250929   -> claude-sonnet-4-5-20250929
    gemini/gemini-2.0-flash@001            -> gemini-2.0-flash-001

A version that is not a snapshot (e.g. "stable" for ids the provider already pins,
such as gemini-2.5-pro or claude-opus-5-5, or Zhipu's "api-v4") is a label and the
model name is sent unchanged. Dated versions can be ranges too: `>=2024-08-06` is
"this snapshot or newer" (see versions.py).

Model lists go stale: update CATALOG when providers ship or retire models.
"""

import re

# Version label for model ids that are already a fixed snapshot.
STABLE = "stable"

CATALOG: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "snapshot": r"\d{4}-\d{2}-\d{2}",
        "models": [
            {"model": "gpt-5.1", "versions": ["2025-11-13"], "label": "GPT-5.1"},
            {"model": "gpt-5", "versions": ["2025-08-07"], "label": "GPT-5"},
            {"model": "gpt-5-mini", "versions": ["2025-08-07"], "label": "GPT-5 mini"},
            {"model": "gpt-4.1", "versions": ["2025-04-14"], "label": "GPT-4.1"},
            {"model": "gpt-4.1-mini", "versions": ["2025-04-14"], "label": "GPT-4.1 mini"},
            {"model": "o4-mini", "versions": ["2025-04-16"], "label": "o4-mini"},
            {"model": "gpt-4o", "versions": ["2024-11-20", "2024-08-06"], "label": "GPT-4o"},
            {"model": "gpt-4o-mini", "versions": ["2024-07-18"], "label": "GPT-4o mini"},
        ],
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_env": "ANTHROPIC_API_KEY",
        "snapshot": r"\d{8}",
        "models": [
            {"model": "claude-opus-5-5", "versions": [STABLE], "label": "Claude Opus 5.5"},
            {"model": "claude-sonnet-5-5", "versions": [STABLE], "label": "Claude Sonnet 5.5"},
            {"model": "claude-haiku-5-5", "versions": [STABLE], "label": "Claude Haiku 5.5"},
            {"model": "claude-opus-4-5", "versions": ["20251101"], "label": "Claude Opus 4.5"},
            {"model": "claude-sonnet-4-5", "versions": ["20250929"], "label": "Claude Sonnet 4.5"},
            {"model": "claude-haiku-4-5", "versions": ["20251001"], "label": "Claude Haiku 4.5"},
            {"model": "claude-opus-4-1", "versions": ["20250805"], "label": "Claude Opus 4.1"},
            {"model": "claude-sonnet-4", "versions": ["20250514"], "label": "Claude Sonnet 4"},
        ],
    },
    "gemini": {
        "label": "Google Gemini",
        "key_env": "GEMINI_API_KEY",
        "key_env_fallbacks": ["GOOGLE_API_KEY"],
        "snapshot": r"\d{3}",
        "models": [
            {"model": "gemini-2.5-pro", "versions": [STABLE], "label": "Gemini 2.5 Pro"},
            {"model": "gemini-2.5-flash", "versions": [STABLE], "label": "Gemini 2.5 Flash"},
            {"model": "gemini-2.5-flash-lite", "versions": [STABLE], "label": "Gemini 2.5 Flash-Lite"},
            {"model": "gemini-2.0-flash", "versions": ["001"], "label": "Gemini 2.0 Flash"},
        ],
    },
    "zhipu": {
        "label": "Zhipu (GLM)",
        "key_env": "ZHIPU_API_KEY",
        "snapshot": None,
        "models": [
            {"model": "glm-4.6", "versions": ["api-v4"], "label": "GLM-4.6"},
            {"model": "glm-4-plus", "versions": ["api-v4"], "label": "GLM-4 Plus"},
        ],
    },
    "mock": {
        "label": "Mock (local, no key)",
        "key_env": None,
        "snapshot": None,
        "models": [
            {"model": "deterministic-reviewer-v1", "versions": ["1"], "label": "Deterministic reviewer v1"},
            {"model": "deterministic-reviewer", "versions": ["1.4.2", "1.2.0"], "label": "Deterministic reviewer"},
        ],
    },
}

# Names people naturally type, mapped to the provider names policies use.
PROVIDER_ALIASES = {"claude": "anthropic", "google": "gemini", "glm": "zhipu", "gpt": "openai"}


def canonical_provider(provider: str) -> str:
    p = provider.strip().lower()
    return PROVIDER_ALIASES.get(p, p)


def snapshot_pattern(provider: str) -> str | None:
    return (CATALOG.get(provider) or {}).get("snapshot")


def api_model_id(model: str, version: str | None, snapshot: str | None) -> str:
    """The model id sent to the provider: `model-version` when `version` is a snapshot
    of the provider's form (`snapshot` regex), otherwise `model` unchanged."""
    if snapshot and version and re.fullmatch(snapshot, version):
        return f"{model}-{version}"
    return model


def catalog_entry(provider: str, model: str) -> dict | None:
    for m in (CATALOG.get(provider) or {}).get("models", []):
        if m["model"] == model:
            return m
    return None


def known_versions(provider: str, model: str) -> list[str]:
    """Snapshots the catalog knows for a model, newest first ([] if unknown)."""
    entry = catalog_entry(provider, model)
    return list(entry["versions"]) if entry else []


def key_envs(provider: str) -> list[str]:
    """Environment variables that may hold the contributor's key for `provider`, in
    order. [] for keyless providers. Unknown providers use <PROVIDER>_API_KEY."""
    p = CATALOG.get(provider)
    if p is not None:
        return [p["key_env"], *p.get("key_env_fallbacks", [])] if p["key_env"] else []
    return [re.sub(r"[^A-Za-z0-9]", "_", provider).upper() + "_API_KEY"]


def catalog_choices() -> list[dict]:
    """Flat list for pickers: one row per provider/model@version, newest first."""
    out = []
    for provider, p in CATALOG.items():
        for m in p["models"]:
            for v in m["versions"]:
                out.append({
                    "provider": provider, "provider_label": p["label"], "model": m["model"],
                    "label": m["label"], "version": v, "spec": f"{provider}/{m['model']}@{v}",
                    "api_model": api_model_id(m["model"], v, p["snapshot"]),
                })
    return out
