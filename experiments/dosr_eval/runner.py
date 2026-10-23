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
    budget: float | None = None  # stop before the next call once this much (pricing currency) is spent


def load_pricing(path: Path | None) -> dict:
    """{"provider/model@version": {"input_per_mtok": float, "output_per_mtok": float, "currency": str}}"""
    if not path:
        return {}
    return json.loads(Path(path).read_text("utf-8"))


def cost_of(usage: dict | None, price: dict | None) -> float | None:
    if not usage or not price:
        return None
    pin, pout = price.get("input_per_mtok"), price.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1_000_000


# Output tokens assumed per call when estimating cost before a run: the attestor's
# max_tokens. Reasoning models may use more (their limit is raised to 8192).
EST_OUTPUT_TOKENS = 1024


def estimate_cost(inputs: dict, models: list[ModelSpec], reps: int, pricing: dict) -> dict[str, float | None]:
    """Upper-bound-ish cost per model: estimated prompt tokens + EST_OUTPUT_TOKENS per call."""
    out = {}
    for spec in models:
        name = str(spec)
        if not spec.paid:
            out[name] = 0.0
            continue
        usage = {"prompt_tokens": sum(ri.metrics["total"]["est_tokens"] for ri in inputs.values()) * reps,
                 "completion_tokens": EST_OUTPUT_TOKENS * len(inputs) * reps}
        out[name] = cost_of(usage, pricing.get(name))
    return out


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
        if cfg.budget is not None and b.spec.paid and cost_of({"prompt_tokens": 1}, cfg.pricing.get(name)) is None:
            raise SystemExit(f"--budget needs prices for {name} in the pricing file")

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
    meta_path = cfg.out_dir / f"{run_id}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    jobs = [(case, tid, name, backend, rep) for case in cfg.cases for tid in cfg.templates
            for name, backend in backends.items() for rep in range(cfg.reps)]
    spent = 0.0
    with rows_path.open("w", encoding="utf-8") as f:
        for done, (case, tid, name, backend, rep) in enumerate(jobs, 1):
            if cfg.budget is not None and spent >= cfg.budget:
                log(f"budget {cfg.budget} reached (spent {spent:.4f}); stopping before call {done}/{len(jobs)}")
                meta.update(stopped="budget", calls_made=done - 1)
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                break
            ri = inputs[(case.id, tid)]
            result = review_once(backend, ri.prompt)
            cost = cost_of(result.get("usage"), cfg.pricing.get(name))
            spent += cost or 0.0
            row = {
                "run_id": run_id,
                "case": case.id,
                "dataset": case.source.get("dataset", case.change["type"]),
                "category": case.category,
                "expected": case.expected,
                "target_issue": case.target_issue,  # for grading only; never in the prompt
                "template": tid,
                "model": name,
                "rep": rep,
                "review_input_hash": ri.review_input_hash,
                "pr_hash": ri.pr_hash,
                "prompt_est_tokens": ri.metrics["total"]["est_tokens"],
                "limit_problems": ri.limit_problems,
                **result,
                "cost": cost,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            verdict = "ERROR" if "error" in result else (
                "parse-fail" if result.get("parse_error") else
                ("approve" if result["approved"] else "reject"))
            log(f"[{done}/{len(jobs)}] {case.id} · {tid} · {name} · rep {rep}: {verdict} "
                f"(expected {case.expected})" + (f" · spent {spent:.4f}" if cfg.pricing else ""))
    return rows_path
