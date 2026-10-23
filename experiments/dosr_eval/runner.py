"""Runs cases x templates x models x repetitions and writes one JSONL row per model call.

A row is everything needed to score the call later without re-running it: the
expected verdict, the parsed decision (or why parsing failed: that is a rejection, as
in the attestor), the raw output's hash, token usage, latency, and cost. The prompt
itself is not stored (it is rebuildable from the case and its `review_input_hash`).
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dosr_protocol.canonical import sha256_0x
from dosr_protocol.decision import DECISION_V1, DecisionError, parse_decision

from .cases import Case, materialize
from .prompts import ReviewInput, build_review_input, load_template
from .reviewers import Backend, ModelSpec, api_key_env, attestor_reviewers
from .spend import cost_of, price_of, worst_case_call

EXPERIMENTS = Path(__file__).resolve().parents[1]


@dataclass
class RunConfig:
    cases: list[Case]
    templates: list[str]
    models: list[ModelSpec]
    reps: int = 1
    out_dir: Path = EXPERIMENTS / "results"
    work_dir: Path = EXPERIMENTS / ".cache" / "work"
    cache_dir: Path = EXPERIMENTS / ".cache" / "git"
    label: str = ""
    pricing: dict = field(default_factory=dict)
    providers_file: Path | None = None
    budget: float | None = None  # stop before spending more than this (pricing currency)


def load_pricing(path: Path | None) -> dict:
    """{"provider/model@version": {"input_per_mtok": float, "output_per_mtok": float, "currency": str}}"""
    if not path:
        return {}
    return json.loads(Path(path).read_text("utf-8"))


def estimate_cost(cfg: RunConfig, inputs: dict) -> float | None:
    """Worst-case cost of the whole run; None if a paid model has no price."""
    total = 0.0
    for spec in cfg.models:
        if spec.provider == "mock":
            continue
        price = price_of(cfg.pricing, str(spec))
        if price is None:
            return None
        total += sum(worst_case_call(ri.metrics["total"]["est_tokens"], price) for ri in inputs.values()) * cfg.reps
    return total


def _git_head() -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=EXPERIMENTS, capture_output=True, text=True)
    return proc.stdout.strip() or None


def build_inputs(cfg: RunConfig) -> dict[tuple[str, str], ReviewInput]:
    """(case id, template id) -> prompt. Built once, reused across models and reps."""
    templates = {t: load_template(t) for t in cfg.templates}
    out = {}
    for case in cfg.cases:
        m = materialize(case, cfg.work_dir, cfg.cache_dir)
        for tid, template in templates.items():
            out[(case.id, tid)] = build_review_input(m, template)
    return out


def review_once(backend: Backend, prompt: str) -> dict:
    """One model call, scored the way the attestor scores it."""
    rv = attestor_reviewers()
    t0 = time.perf_counter()
    try:
        raw, usage = backend.complete(prompt)
    except rv.ReviewerError as e:
        return {"error": str(e), "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    latency = round((time.perf_counter() - t0) * 1000, 1)
    row = {"latency_ms": latency, "usage": usage, "response_hash": sha256_0x(raw.encode("utf-8")),
           "parse_error": None}
    try:
        decision = parse_decision(raw, DECISION_V1)
        row.update(approved=decision["approved"], summary=decision["summary"], issues=decision["issues"])
    except DecisionError as e:
        # The attestor turns unparseable output into a signed rejection; so do we.
        row.update(approved=False, summary=None, issues=[], parse_error=str(e), raw_output=raw[:4000])
    return row


def run(cfg: RunConfig, inputs: dict | None = None, backends: dict | None = None, log=print) -> Path:
    inputs = inputs if inputs is not None else build_inputs(cfg)
    backends = backends or {str(s): Backend(s, cfg.providers_file) for s in cfg.models}
    for name, b in backends.items():
        if b.needs_key and not b.key:
            raise SystemExit(f"{name} needs an API key: set {api_key_env(b.spec.provider)} "
                             f"(environment or the repo's .env)")
        if cfg.budget is not None and b.needs_key and price_of(cfg.pricing, name) is None:
            raise SystemExit(f"--budget needs a price for {name} in the pricing file")

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + (f"-{cfg.label}" if cfg.label else "")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = cfg.out_dir / f"{run_id}.jsonl"
    meta = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "dosr_commit": _git_head(),
        "cases": [c.id for c in cfg.cases],
        "templates": cfg.templates,
        "models": [str(s) for s in cfg.models],
        "reps": cfg.reps,
        "decision_schema": DECISION_V1,
        "budget": cfg.budget,
    }
    (cfg.out_dir / f"{run_id}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    total = len(cfg.cases) * len(cfg.templates) * len(backends) * cfg.reps
    done, spent = 0, 0.0

    def stop(reason: str) -> Path:
        meta.update(stopped=reason, calls_made=done, spent=round(spent, 6))
        (cfg.out_dir / f"{run_id}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log(f"stopped: {reason}")
        return rows_path

    with rows_path.open("w", encoding="utf-8") as f:
        for case in cfg.cases:
            for tid in cfg.templates:
                ri = inputs[(case.id, tid)]
                for name, backend in backends.items():
                    price = price_of(cfg.pricing, name)
                    for rep in range(cfg.reps):
                        if cfg.budget is not None and price is not None:
                            next_max = worst_case_call(ri.metrics["total"]["est_tokens"], price)
                            if spent + next_max > cfg.budget:
                                return stop(f"budget {cfg.budget} reached ({spent:.4f} spent, next call "
                                            f"could cost up to {next_max:.4f})")
                        result = review_once(backend, ri.prompt)
                        row = {
                            "run_id": run_id,
                            "case": case.id,
                            "category": case.category,
                            "cell": case.cell,
                            "expected": case.expected,
                            "template": tid,
                            "model": name,
                            "rep": rep,
                            "review_input_hash": ri.review_input_hash,
                            "pr_hash": ri.pr_hash,
                            "prompt_est_tokens": ri.metrics["total"]["est_tokens"],
                            "limit_problems": ri.limit_problems,
                            **result,
                            "cost": cost_of(result.get("usage"), price),
                        }
                        spent += row["cost"] or 0.0
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        f.flush()
                        done += 1
                        verdict = "ERROR" if "error" in result else (
                            "parse-fail" if result.get("parse_error") else
                            ("approve" if result["approved"] else "reject"))
                        log(f"[{done}/{total}] {case.id} · {tid} · {name} · rep {rep}: {verdict} "
                            f"(expected {case.expected})" + (f" · spent {spent:.4f}" if price else ""))
    meta.update(calls_made=done, spent=round(spent, 6))
    (cfg.out_dir / f"{run_id}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return rows_path
