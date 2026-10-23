"""Model backends: the attestor's own classes, plus token-usage capture.

We import services/attestor/attestor/reviewers.py rather than reimplementing the call,
so the request body (temperature 0, JSON-object output, max_tokens, provider extras)
is exactly what production sends, for every provider the attestor knows: OpenAI,
Gemini and Zhipu (chat-completions) and Anthropic (Messages API). The attestor
discards the provider's `usage` field; we read it from the HTTP response with an httpx
event hook, without changing the attestor's code, and normalize it to
{"prompt_tokens", "completion_tokens"}.

API keys come from the same variables the DOSR client reads (OPENAI_API_KEY,
ANTHROPIC_API_KEY, GEMINI_API_KEY, ZHIPU_API_KEY, ...; see dosr_protocol.models), from
the environment or the repo's .env. Keys are never written to results.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
from dosr_protocol.models import key_envs

ROOT = Path(__file__).resolve().parents[2]
ATTESTOR_DIR = ROOT / "services" / "attestor"

# Providers that cost nothing to call. Anything else is a paid call and needs --paid.
FREE_PROVIDERS = {"mock"}


def attestor_reviewers():
    """The attestor's reviewers module (the attestor is an app, not an installed package)."""
    if str(ATTESTOR_DIR) not in sys.path:
        sys.path.insert(0, str(ATTESTOR_DIR))
    from attestor import reviewers

    return reviewers


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    version: str

    @classmethod
    def parse(cls, spec: str) -> "ModelSpec":
        """`provider/model@version`, e.g. `zhipu/glm-4.6@api-v4`."""
        provider, sep, rest = spec.partition("/")
        model, sep2, version = rest.partition("@")
        if not (sep and sep2 and provider and model and version):
            raise ValueError(f"model must look like provider/model@version, got {spec!r}")
        return cls(provider, model, version)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}@{self.version}"

    def as_review(self):
        # What reviewers.*.complete() reads from the request's `review` block.
        return SimpleNamespace(provider=self.provider, model=self.model, model_version=self.version)

    @property
    def paid(self) -> bool:
        return self.provider not in FREE_PROVIDERS


def normalize_usage(usage) -> dict | None:
    """Chat-completions `usage` as is; Anthropic's input/output_tokens renamed to match."""
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return usage
    if "input_tokens" in usage or "output_tokens" in usage:
        # Cached prompt tokens are billed too; count them as prompt tokens.
        prompt = sum(usage.get(k) or 0 for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return {"prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens") or 0, "raw": usage}
    return usage


class UsageRecorder:
    """httpx response hook: keeps the (normalized) `usage` object of the last model reply."""

    def __init__(self):
        self.last: dict | None = None

    def reset(self) -> None:
        self.last = None

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        try:
            usage = response.json().get("usage")
        except (ValueError, AttributeError):
            usage = None
        self.last = normalize_usage(usage)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Same rules as demo/demo.py: values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def api_key_env(provider: str) -> str:
    """The variable to set for `provider` (the first one the DOSR client looks at)."""
    envs = key_envs(provider)
    return envs[0] if envs else ""


def find_key(provider: str) -> str | None:
    return next((os.environ[e] for e in key_envs(provider) if os.environ.get(e)), None)


class Backend:
    def __init__(self, spec: ModelSpec, providers_file: Path | None = None, timeout: float = 120.0,
                 http: httpx.Client | None = None):
        rv = attestor_reviewers()
        self.spec = spec
        self.usage = UsageRecorder()
        if spec.provider == "mock":
            self.reviewer = rv.MockReviewer()
        else:
            providers = rv.load_providers(providers_file)
            if spec.provider not in providers:
                raise ValueError(f"unknown provider {spec.provider!r} (known: mock, {', '.join(sorted(providers))})")
            provider = providers[spec.provider]
            if provider.api not in rv.BACKENDS:
                raise ValueError(f"provider {spec.provider!r}: unknown api {provider.api!r}")
            if http is None:
                http = httpx.Client(timeout=timeout)
            hooks = http.event_hooks
            http.event_hooks = {**hooks, "response": [*hooks.get("response", []), self.usage]}
            # The attestor's backend class for this provider's API (chat-completions or
            # anthropic-messages), so the request is what the attestor would send.
            self.reviewer = rv.BACKENDS[provider.api](spec.provider, provider, http=http)
        self.needs_key = self.reviewer.needs_key
        self.key = find_key(spec.provider) if self.needs_key else None

    def complete(self, prompt: str) -> tuple[str, dict | None]:
        """Raw model output (as the attestor would hash it) and the provider's usage, if any."""
        self.usage.reset()
        raw = self.reviewer.complete(prompt, self.spec.as_review(), self.key)
        return raw, self.usage.last
