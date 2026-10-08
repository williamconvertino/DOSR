"""Per-repository step latency history, used for remaining-time estimates."""

import json
from pathlib import Path

# Rough first-run guesses (ms) before any history exists.
DEFAULT_ESTIMATES_MS = {
    "preflight": 60,
    "commit": 150,
    "parent": 40,
    "bundle": 150,
    "pr": 200,
    "request": 5,
    "upload": 30,
    "await": 400,
    "verify": 5,
    "register": 20,
}

_ALPHA = 0.3  # exponential moving average weight of the newest sample


class TimingStore:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def estimate_ms(self, step_id: str) -> float:
        entry = self.data.get(step_id)
        if entry:
            return entry["ema_ms"]
        return DEFAULT_ESTIMATES_MS.get(step_id, 100)

    def record(self, step_id: str, ms: float) -> None:
        entry = self.data.get(step_id)
        if entry:
            entry["ema_ms"] = _ALPHA * ms + (1 - _ALPHA) * entry["ema_ms"]
            entry["samples"] += 1
            entry["last_ms"] = ms
        else:
            self.data[step_id] = {"ema_ms": ms, "samples": 1, "last_ms": ms}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass  # estimates are best-effort
