"""CLI: python -m dosr_eval {list,prompt,run,report} --help"""

import argparse
import sys
from pathlib import Path

from .cases import CaseError, load_cases, materialize
from .metrics import load_rows, report
from .prompts import build_review_input, load_template
from .reviewers import ModelSpec, load_dotenv
from .runner import EXPERIMENTS, RunConfig, build_inputs, load_pricing, run

DEFAULT_TEMPLATE = "security-sensitive-review-v2"
DEFAULT_MODEL = "mock/deterministic-reviewer-v1@1"


def cmd_list(args) -> int:
    for c in load_cases(args.cases):
        src = c.source.get("dataset", c.change["type"])
        print(f"{c.id:32} {c.category:10} expect {c.expected:8} {src}")
    return 0


def cmd_prompt(args) -> int:
    (case,) = load_cases(args.cases, [args.case])
    m = materialize(case, EXPERIMENTS / ".cache" / "work", EXPERIMENTS / ".cache" / "git")
    ri = build_review_input(m, load_template(args.template))
    print(ri.prompt)
    print(f"\n--- review_input_hash {ri.review_input_hash} · ~{ri.metrics['total']['est_tokens']:,} tokens"
          + (f" · ⚠ {'; '.join(ri.limit_problems)}" if ri.limit_problems else ""), file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    cfg = RunConfig(
        cases=load_cases(args.cases, args.case or None),
        templates=args.template or [DEFAULT_TEMPLATE],
        models=[ModelSpec.parse(m) for m in (args.model or [DEFAULT_MODEL])],
        reps=args.reps,
        out_dir=args.out,
        label=args.label,
        pricing=load_pricing(args.pricing),
        providers_file=args.providers_file,
    )
    inputs = build_inputs(cfg)
    calls = len(inputs) * len(cfg.models) * cfg.reps
    est = sum(ri.metrics["total"]["est_tokens"] for ri in inputs.values()) * len(cfg.models) * cfg.reps
    print(f"{len(cfg.cases)} case(s) × {len(cfg.templates)} template(s) × {len(cfg.models)} model(s) × "
          f"{cfg.reps} rep(s) = {calls} call(s), ~{est:,} prompt tokens (chars/4 estimate)")
    for (cid, tid), ri in inputs.items():
        if ri.limit_problems:
            print(f"  ⚠ {cid} · {tid}: the attestor would refuse before the model: {'; '.join(ri.limit_problems)}")
    if args.dry_run:
        return 0
    path = run(cfg, inputs=inputs)
    print(f"\nresults: {path}\n")
    print(report(load_rows(path)))
    return 0


def cmd_report(args) -> int:
    text = report(load_rows(args.results))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dosr_eval", description="DOSR review evaluation harness")
    ap.add_argument("--cases", type=Path, default=EXPERIMENTS / "cases", help="cases directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list cases").set_defaults(fn=cmd_list)

    p = sub.add_parser("prompt", help="print the exact prompt a case produces (no model call)")
    p.add_argument("case")
    p.add_argument("--template", default=DEFAULT_TEMPLATE)
    p.set_defaults(fn=cmd_prompt)

    p = sub.add_parser("run", help="review cases and write results JSONL")
    p.add_argument("--case", action="append", help="case id (repeatable; default: all)")
    p.add_argument("--template", action="append", help=f"template id (repeatable; default {DEFAULT_TEMPLATE})")
    p.add_argument("--model", action="append", help=f"provider/model@version (repeatable; default {DEFAULT_MODEL})")
    p.add_argument("--reps", type=int, default=1, help="calls per case/template/model")
    p.add_argument("--out", type=Path, default=EXPERIMENTS / "results")
    p.add_argument("--label", default="", help="suffix for the results file name")
    p.add_argument("--pricing", type=Path, help="JSON of per-model token prices (see pricing.example.json)")
    p.add_argument("--providers-file", type=Path, help="extra OpenAI-compatible providers (attestor format)")
    p.add_argument("--dry-run", action="store_true", help="build prompts and estimate size; call nothing")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="score a results JSONL file")
    p.add_argument("results", type=Path)
    p.add_argument("--out", type=Path, help="also write the report to this file")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    load_dotenv()
    try:
        return args.fn(args)
    except CaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
