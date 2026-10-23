"""Model backends. Each returns the model's raw text; review.py hashes and parses it.

A backend is anything with `complete(prompt, model, api_key) -> str` and a `needs_key`
attribute. Built in: OpenAI, Google Gemini and Zhipu (all OpenAI-compatible
chat-completions, `ChatCompletionsReviewer`) and Anthropic Claude (Messages API,
`AnthropicReviewer`). Other providers (DeepSeek, Qwen, Moonshot, ...) need only a
`Provider` entry: in `BUILTIN_PROVIDERS`, or without code changes in a JSON file named
by ATTESTOR_PROVIDERS_FILE:

    {"deepseek": {"url": "https://api.deepseek.com/chat/completions"},
     "my-claude-proxy": {"url": "https://llm.example/v1/messages", "api": "anthropic-messages"}}

The model id sent upstream is `model-version` when `version` is a dated snapshot of the
provider's form (`openai/gpt-4o@2024-08-06` -> `gpt-4o-2024-08-06`), else `model`
(see dosr_protocol.models).

The contributor pays for inference: their provider API key arrives with the request
(X-DOSR-Provider-Key), is used for this one call, and is never stored, logged,
hashed, or returned.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dosr_protocol.models import api_model_id, snapshot_pattern


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
    """A model API endpoint. `api` picks the backend class (see BACKENDS)."""

    url: str
    api: str = "chat-completions"  # or "anthropic-messages"
    # Extra request fields: sent on every call, and per model-name prefix (in order;
    # later prefixes win). A value of None removes the field, e.g. `temperature` for
    # reasoning models that reject it.
    extra_body: dict = field(default_factory=dict)
    model_extra: dict[str, dict] = field(default_factory=dict)
    # Regex for dated snapshot versions that are appended to the model name.
    snapshot: str | None = None
    # Output token limit field: newer OpenAI models only accept max_completion_tokens.
    token_param: str = "max_tokens"


_THINKING_OFF = {"thinking": {"type": "disabled"}}
# OpenAI reasoning models reject temperature != 1, and reasoning tokens count against
# the output limit, so give them room and ask for little reasoning.
_OPENAI_REASONING = {"temperature": None, "reasoning_effort": "low", "max_completion_tokens": 8192}
# Gemini 2.5+ think by default; thinking tokens count against max_tokens.
_GEMINI_THINKING = {"reasoning_effort": "low", "max_tokens": 8192}
# Claude 5 models: sampling parameters are left at the provider default.
_CLAUDE_DEFAULT_SAMPLING = {"temperature": None}

BUILTIN_PROVIDERS = {
    # https://open.bigmodel.cn. No dated model snapshots, so policies pin the API
    # version ("api-v4"). Thinking models are switched off so `content` is the answer.
    "zhipu": Provider(
        url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        model_extra={"glm-4.5": _THINKING_OFF, "glm-4.6": _THINKING_OFF, "glm-5": _THINKING_OFF},
    ),
    "openai": Provider(
        url="https://api.openai.com/v1/chat/completions",
        snapshot=snapshot_pattern("openai"),
        token_param="max_completion_tokens",
        model_extra={p: _OPENAI_REASONING for p in ("gpt-5", "o1", "o3", "o4")},
    ),
    # Gemini's OpenAI-compatible endpoint (same API key as the native one).
    "gemini": Provider(
        url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        snapshot=snapshot_pattern("gemini"),
        model_extra={p: _GEMINI_THINKING for p in ("gemini-2.5", "gemini-3")},
    ),
    "anthropic": Provider(
        url="https://api.anthropic.com/v1/messages",
        api="anthropic-messages",
        snapshot=snapshot_pattern("anthropic"),
        model_extra={p: _CLAUDE_DEFAULT_SAMPLING
                     for p in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable")},
    ),
}


def _apply_extra(body: dict, provider: Provider, model_name: str) -> dict:
    extras = [provider.extra_body] + [e for p, e in provider.model_extra.items() if model_name.startswith(p)]
    for extra in extras:
        for k, v in extra.items():
            if v is None:
                body.pop(k, None)
            else:
                body[k] = v
    return body


def _model_id(provider: Provider, model) -> str:
    return api_model_id(model.model, getattr(model, "model_version", None), provider.snapshot)


class _HTTPReviewer:
    needs_key = True

    def __init__(self, name: str, provider: Provider, timeout: float = 120.0, http: httpx.Client | None = None):
        self.name = name
        self.provider = provider
        self.http = http or httpx.Client(timeout=timeout)

    def _post(self, api_key: str | None, headers: dict, body: dict) -> dict:
        if not api_key:
            raise MissingProviderKey(f"provider {self.name} needs the contributor's API key (X-DOSR-Provider-Key)")
        try:
            r = self.http.post(self.provider.url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise ReviewerError(f"cannot reach {self.name}: {type(e).__name__}")
        if r.status_code != 200:
            # Provider error bodies do not contain the key; keep only a short excerpt.
            raise ReviewerError(f"{self.name} returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            raise ReviewerError(f"{self.name} returned an unexpected response shape")


class ChatCompletionsReviewer(_HTTPReviewer):
    """Calls an OpenAI-compatible provider with fixed parameters (part of what the
    attestor vouches for): the prompt as a single user message, temperature 0,
    JSON-object output, plus the provider's own extra fields."""

    def complete(self, prompt: str, model, api_key: str | None = None) -> str:
        body = _apply_extra({
            "model": _model_id(self.provider, model),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            self.provider.token_param: 1024,
            "response_format": {"type": "json_object"},
        }, self.provider, model.model)
        data = self._post(api_key, {"Authorization": f"Bearer {api_key}"}, body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ReviewerError(f"{self.name} returned an unexpected response shape")


class AnthropicReviewer(_HTTPReviewer):
    """Calls Anthropic's Messages API with fixed parameters: the prompt as a single
    user message, temperature 0 (unless the model's extras drop it). Claude has no
    JSON mode; the prompt asks for exactly one JSON object and decision.py accepts
    that (optionally inside one ``` fence)."""

    API_VERSION = "2023-06-01"

    def complete(self, prompt: str, model, api_key: str | None = None) -> str:
        body = _apply_extra({
            "model": _model_id(self.provider, model),
            "max_tokens": 1024,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }, self.provider, model.model)
        data = self._post(api_key, {"x-api-key": api_key or "", "anthropic-version": self.API_VERSION}, body)
        try:
            text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError):
            raise ReviewerError(f"{self.name} returned an unexpected response shape")
        if not text:
            raise ReviewerError(f"{self.name} returned no text")
        return text


BACKENDS = {"chat-completions": ChatCompletionsReviewer, "anthropic-messages": AnthropicReviewer}


def load_providers(path: Path | None) -> dict[str, Provider]:
    """Built-in providers, overridden/extended by the JSON file at `path`."""
    providers = dict(BUILTIN_PROVIDERS)
    if path:
        for name, cfg in json.loads(Path(path).read_text("utf-8")).items():
            providers[name] = Provider(**cfg)
    return providers


def default_reviewers(providers_file: Path | None = None) -> dict:
    """provider name -> backend. A provider missing here cannot be reviewed by this attestor."""
    reviewers = {}
    for name, p in load_providers(providers_file).items():
        if p.api not in BACKENDS:
            raise ValueError(f"provider {name}: unknown api {p.api!r} (one of {', '.join(BACKENDS)})")
        reviewers[name] = BACKENDS[p.api](name, p)
    return {"mock": MockReviewer(), **reviewers}
