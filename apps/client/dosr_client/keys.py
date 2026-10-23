"""Contributor API keys for review-model providers (OpenAI, Anthropic, Gemini, ...).

The client reads keys from the environment at submit time (config.provider_api_key),
so setting one here takes effect immediately, without a restart. Keys are optionally
persisted to a .env file (git-ignored), the same file `demo.py` loads at startup.

A key is never returned in full: callers only ever see `mask(key)` (the last 4
characters), so the GUI can be shown on screen without leaking it.
"""

import os
import re
import threading
from pathlib import Path

from dosr_protocol.models import CATALOG, key_envs

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_lock = threading.Lock()


class KeyStoreError(ValueError):
    pass


def mask(key: str | None) -> str | None:
    if not key:
        return None
    return "•" * 8 + (key[-4:] if len(key) >= 12 else "")


def _read_env_file(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    out = {}
    for line in path.read_text("utf-8").splitlines():
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def _write_env_file(path: Path, updates: dict[str, str | None]) -> None:
    """Set (str) or delete (None) variables in a .env file, keeping every other line."""
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    done, out = set(), []
    for line in lines:
        m = _LINE.match(line)
        name = m.group(1) if m and not line.lstrip().startswith("#") else None
        if name in updates:
            if updates[name] is not None and name not in done:
                out.append(f"{name}={updates[name]}")
            done.add(name)
        else:
            out.append(line)
    for name, value in updates.items():
        if name not in done and value is not None:
            out.append(f"{name}={value}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class KeyStore:
    def __init__(self, env_file: Path | None = None):
        self.env_file = Path(env_file).resolve() if env_file else None

    def providers(self) -> list[str]:
        return [p for p in CATALOG if key_envs(p)]

    def status(self) -> list[dict]:
        saved = _read_env_file(self.env_file)
        rows = []
        for provider in self.providers():
            envs = key_envs(provider)
            env = next((e for e in envs if os.environ.get(e)), None)
            value = os.environ.get(env) if env else None
            source = None
            if value:
                source = "saved" if saved.get(env) == value else "environment"
            rows.append({
                "provider": provider, "label": CATALOG[provider]["label"], "env": envs[0], "alt_envs": envs[1:],
                "set": bool(value), "masked": mask(value), "source": source, "set_via": env,
            })
        return rows

    def set(self, provider: str, key: str) -> dict:
        if provider not in self.providers():
            raise KeyStoreError(f"unknown provider {provider!r}")
        key = (key or "").strip()
        if not key or len(key) > 512 or re.search(r"\s", key):
            raise KeyStoreError("the key must be one line of 1..512 non-space characters")
        env = key_envs(provider)[0]
        with _lock:
            os.environ[env] = key
            if self.env_file:
                _write_env_file(self.env_file, {env: key})
        return self._row(provider)

    def remove(self, provider: str) -> dict:
        if provider not in self.providers():
            raise KeyStoreError(f"unknown provider {provider!r}")
        envs = key_envs(provider)
        with _lock:
            for env in envs:
                os.environ.pop(env, None)
            if self.env_file and self.env_file.exists():
                _write_env_file(self.env_file, {env: None for env in envs})
        return self._row(provider)

    def _row(self, provider: str) -> dict:
        return next(r for r in self.status() if r["provider"] == provider)
