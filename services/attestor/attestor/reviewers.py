"""Model backends. Each returns the model's raw text; review.py hashes and parses it.

A backend is anything with `complete(prompt, model, api_key) -> str` and a `needs_key`
attribute. Most providers (Zhipu, OpenAI, DeepSeek, Qwen, Moonshot, ...) expose an
OpenAI-compatible chat-completions endpoint, so they share `ChatCompletionsReviewer`
and differ only in a `Provider` entry. Add one in `BUILTIN_PROVIDERS`, or without code
changes in a JSON file named by ATTESTOR_PROVIDERS_FILE:

    {"deepseek": {"url": "https://api.deepseek.com/chat/completions"}}

A provider with a different API shape needs its own backend class.

The contributor pays for inference: their provider API key arrives with the request
(X-DOSR-Provider-Key), is used for this one call, and is never stored, logged,
hashed, or returned.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx


class ReviewerError(Exception):
    """The model could not be called (network, auth, quota). Not a review decision."""


class MissingProviderKey(ReviewerError):
    pass


class MockReviewer:
    """Deterministic stand-in: approves anything that passed the checks."""

    needs_key = False

    def complete(self, prompt: str, model, api_key: str | None = None) -> str:
        return json.dumps({
            "approved": True,
            "summary": "No policy-blocking issues were found in the proposed transition.",
            "issues": [],
        })


@dataclass(frozen=True)
class Provider:
    """An OpenAI-compatible chat-completions endpoint."""

    url: str
    # Extra request fields: sent on every call, and per model-name prefix.
    extra_body: dict = field(default_factory=dict)
    model_extra: dict[str, dict] = field(default_factory=dict)


_THINKING_OFF = {"thinking": {"type": "disabled"}}

BUILTIN_PROVIDERS = {
    # https://open.bigmodel.cn. No dated model snapshots, so policies pin the API
    # version ("api-v4"). Thinking models are switched off so `content` is the answer.
    "zhipu": Provider(
        url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        model_extra={"glm-4.5": _THINKING_OFF, "glm-4.6": _THINKING_OFF, "glm-5": _THINKING_OFF},
    ),
}


class ChatCompletionsReviewer:
    """Calls an OpenAI-compatible provider with fixed parameters (part of what the
    attestor vouches for): the prompt as a single user message, temperature 0,
    JSON-object output, plus the provider's own extra fields."""

    needs_key = True

    def __init__(self, name: str, provider: Provider, timeout: float = 120.0, http: httpx.Client | None = None):
        self.name = name
        self.provider = provider
        self.http = http or httpx.Client(timeout=timeout)

    def complete(self, prompt: str, model, api_key: str | None = None) -> str:
        if not api_key:
            raise MissingProviderKey(f"provider {self.name} needs the contributor's API key (X-DOSR-Provider-Key)")
        body = {
            "model": model.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **self.provider.extra_body,
        }
        for prefix, extra in self.provider.model_extra.items():
            if model.model.startswith(prefix):
                body.update(extra)
        try:
            r = self.http.post(self.provider.url, headers={"Authorization": f"Bearer {api_key}"}, json=body)
        except httpx.HTTPError as e:
            raise ReviewerError(f"cannot reach {self.name}: {type(e).__name__}")
        if r.status_code != 200:
            # Provider error bodies do not contain the key; keep only a short excerpt.
            raise ReviewerError(f"{self.name} returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ReviewerError(f"{self.name} returned an unexpected response shape")


def load_providers(path: Path | None) -> dict[str, Provider]:
    """Built-in providers, overridden/extended by the JSON file at `path`."""
    providers = dict(BUILTIN_PROVIDERS)
    if path:
        for name, cfg in json.loads(Path(path).read_text("utf-8")).items():
            providers[name] = Provider(**cfg)
    return providers


def default_reviewers(providers_file: Path | None = None) -> dict:
    """provider name -> backend. A provider missing here cannot be reviewed by this attestor."""
    reviewers = {name: ChatCompletionsReviewer(name, p) for name, p in load_providers(providers_file).items()}
    return {"mock": MockReviewer(), **reviewers}
