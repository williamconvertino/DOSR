"""Model backends: the attestor's own classes, plus token-usage capture.

We import services/attestor/attestor/reviewers.py rather than reimplementing the call,
so the request body (temperature 0, JSON-object output, max_tokens, provider extras)
is exactly what production sends. The attestor discards the provider's `usage` field;
we read it from the HTTP response with an httpx event hook, without changing the
attestor's code.

API keys come from `<PROVIDER>_API_KEY` (e.g. ZHIPU_API_KEY), the same variable the
DOSR client uses, read from the environment or the repo's .env. Keys are never written
to results.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[2]
ATTESTOR_DIR = ROOT / "services" / "attestor"


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


class UsageRecorder:
    """httpx response hook: keeps the `usage` object of the last chat-completions reply."""

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
        self.last = usage if isinstance(usage, dict) else None


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Same rules as demo/demo.py: values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def api_key_env(provider: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in provider).upper() + "_API_KEY"


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
            if http is None:
                http = httpx.Client(timeout=timeout)
            hooks = http.event_hooks
            http.event_hooks = {**hooks, "response": [*hooks.get("response", []), self.usage]}
            self.reviewer = rv.ChatCompletionsReviewer(spec.provider, providers[spec.provider], http=http)
        self.needs_key = self.reviewer.needs_key
        self.key = os.environ.get(api_key_env(spec.provider)) if self.needs_key else None

    def complete(self, prompt: str) -> tuple[str, dict | None]:
        """Raw model output (as the attestor would hash it) and the provider's usage, if any."""
        self.usage.reset()
        raw = self.reviewer.complete(prompt, self.spec.as_review(), self.key)
        return raw, self.usage.last
