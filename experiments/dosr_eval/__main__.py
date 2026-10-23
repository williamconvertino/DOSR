"""CLI: python -m dosr_eval {list,prompt,run,models,report,grade,figures,import-sevra,import-swebench} --help"""

import argparse
import sys
from pathlib import Path

from .cases import CaseError, load_cases, materialize
from .metrics import load_grades, load_rows, report, row_key, write_grade_sheet
from .prompts import build_review_input, load_template
from .reviewers import ModelSpec, api_key_env, attestor_reviewers, find_key, load_dotenv
from .runner import EXPERIMENTS, RunConfig, build_inputs, estimate_cost, load_pricing, run

DEFAULT_TEMPLATE = "security-sensitive-review-v2"
DEFAULT_MODEL = "mock/deterministic-reviewer-v1@1"


def cmd_list(args) -> int:
    cases = load_cases(args.cases)
    width = max((len(c.id) for c in cases), default=0) + 2
    for c in cases:
        src = c.source.get("dataset", c.change["type"])
        print(f"{c.id:{width}} {c.category:10} expect {c.expected:8} {src}")
    return 0


def cmd_prompt(args) -> int:
    (case,) = load_cases(args.cases, [args.case])
    m = materialize(case, EXPERIMENTS / ".cache" / "work", EXPERIMENTS / ".cache" / "git")
    ri = build_review_input(m, load_template(args.template))
    print(ri.prompt)
    print(f"\n--- review_input_hash {ri.review_input_hash} · ~{ri.metrics['total']['est_tokens']:,} tokens"
          + (f" · ⚠ {'; '.join(ri.limit_problems)}" if ri.limit_problems else ""), file=sys.stderr)
    return 0


def cmd_models(args) -> int:
    rv = attestor_reviewers()
    print("provider     api                  key")
    print(f"{'mock':12} {'(built in, free)':20} not needed")
    for name, p in sorted(rv.load_providers(args.providers_file).items()):
        print(f"{name:12} {p.api:20} {'set' if find_key(name) else 'missing: set ' + api_key_env(name)}")
    print("\nModel specs look like provider/model@version, e.g. openai/gpt-5@2025-08-07, "
          "anthropic/<model>@latest, zhipu/glm-4.6@api-v4.\nA version that is not a dated "
          "snapshot is ignored and the model name is sent as is.")
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
        budget=args.budget,
    )
    inputs = build_inputs(cfg)
    calls = len(inputs) * len(cfg.models) * cfg.reps
    est = sum(ri.metrics["total"]["est_tokens"] for ri in inputs.values()) * len(cfg.models) * cfg.reps
    print(f"{len(cfg.cases)} case(s) × {len(cfg.templates)} template(s) × {len(cfg.models)} model(s) × "
          f"{cfg.reps} rep(s) = {calls} call(s), ~{est:,} prompt tokens (chars/4 estimate)")
    for name, cost in estimate_cost(inputs, cfg.models, cfg.reps, cfg.pricing).items():
        price = cfg.pricing.get(name, {})
        shown = "free" if cost == 0 else (f"≤ ~{cost:.4f} {price.get('currency', '')}".rstrip()
                                          if cost is not None else "unknown (no prices in --pricing)")
        print(f"  {name}: estimated cost {shown}")
    for (cid, tid), ri in inputs.items():
        if ri.limit_problems:
            print(f"  ⚠ {cid} · {tid}: the attestor would refuse before the model: {'; '.join(ri.limit_problems)}")
    if args.dry_run:
        return 0
    paid = [str(m) for m in cfg.models if m.paid]
    if paid and not args.paid:
        print(f"\nnot running: {', '.join(paid)} {'is a paid model' if len(paid) == 1 else 'are paid models'}. "
              "Add --paid to make these calls (and --budget to cap the spend).", file=sys.stderr)
        return 2
    path = run(cfg, inputs=inputs)
    print(f"\nresults: {path}\n")
    print(report(load_rows(path)))
    return 0


def cmd_import_sevra(args) -> int:
    from .importers import import_sevra

    for vuln in args.vuln:
        for d in import_sevra(vuln, args.out, args.axis or [], commit=args.commit):
            print(f"wrote {d}")
    return 0


def cmd_import_swebench(args) -> int:
    from .importers import DEFAULT_RUN, import_swebench

    if not args.instance and not args.pick:
        print("error: give --instance ID (repeatable) or --pick N", file=sys.stderr)
        return 1
    for d in import_swebench(args.out, run=args.run or DEFAULT_RUN, ids=args.instance, pick=args.pick,
                             max_lines=args.max_lines, with_tests=args.with_tests, skip=args.skip,
                             repos=args.repo):
        print(f"wrote {d}")
    return 0


def cmd_grade(args) -> int:
    out = args.out or args.results.with_suffix(".grades.csv")
    if out.exists() and not args.force:
        print(f"error: {out} exists (keeps your grades); pass --force to overwrite", file=sys.stderr)
        return 1
    n = write_grade_sheet(load_rows(args.results), out)
    print(f"wrote {out}: {n} correct rejection(s) to grade. Put y or n in found_target, then:\n"
          f"  report {args.results} --grades {out}")
    return 0


def _load(results: list[Path], grades: list[Path] | None, exclude: list[str] | None) -> list[dict]:
    """Rows of one or more runs (e.g. a rerun of replaced cases), with their grade sheets."""
    graded = {}
    for g in grades or []:
        graded.update(load_grades(g))
    rows = [r for path in results for r in load_rows(path)]
    if grades:
        for r in rows:
            r["found_target"] = graded.get(row_key(r))
    return _without(rows, exclude)


def cmd_figures(args) -> int:
    from .paper import write_outputs

    rows = _load(args.results, args.grades, args.exclude)
    name = args.results[0].stem + (f"+{len(args.results) - 1}" if len(args.results) > 1 else "")
    out = args.out or EXPERIMENTS / "reports" / name
    for p in write_outputs(rows, out, currency=args.currency):
        print(f"wrote {p}")
    return 0


def _without(rows: list[dict], exclude: list[str] | None) -> list[dict]:
    """Drop cases whose label turned out to be wrong (raw results stay untouched)."""
    if exclude:
        unknown = set(exclude) - {r["case"] for r in rows}
        if unknown:
            raise CaseError(f"--exclude: no such case in the results: {', '.join(sorted(unknown))}")
        print(f"excluding {len(exclude)} case(s): {', '.join(exclude)}", file=sys.stderr)
    return [r for r in rows if r["case"] not in set(exclude or ())]


def cmd_report(args) -> int:
    text = report(_load(args.results, args.grades, args.exclude))
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
    p.add_argument("--paid", action="store_true",
                   help="allow calls to paid models (anything but mock/); without it only the plan is printed")
    p.add_argument("--budget", type=float,
                   help="stop before the next call once this much is spent (needs --pricing for paid models)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("import-sevra", help="write fix / revert / misleading-revert cases for SEVRA vulns")
    p.add_argument("vuln", nargs="+", help="SEVRA vuln id (GHSA-... or CVE-...)")
    p.add_argument("--out", type=Path, required=True, help="suite directory, e.g. cases/pilot-v1")
    p.add_argument("--axis", action="append",
                   help="SEVRA social-engineering axis for a misleading revert (repeatable), e.g. misleading_hardening")
    p.add_argument("--commit", help="owner/repo@sha of the fix, when the advisory links none or several")
    p.set_defaults(fn=cmd_import_sevra)

    p = sub.add_parser("import-swebench", help="write gold / failing-leaderboard-patch pairs from SWE-bench Verified")
    p.add_argument("--out", type=Path, required=True, help="suite directory, e.g. cases/pilot-v1")
    p.add_argument("--run", default=None, help="leaderboard run under evaluation/verified/ in swe-bench/experiments")
    p.add_argument("--instance", action="append", help="instance id (repeatable)")
    p.add_argument("--pick", type=int, default=0, help="or pick N issues automatically, one per repository")
    p.add_argument("--max-lines", type=int, default=60, help="skip patches changing more lines than this (--pick)")
    p.add_argument("--with-tests", action="store_true", help="include the upstream test changes in the gold patch")
    p.add_argument("--skip", action="append", help="instance id never to pick (repeatable)")
    p.add_argument("--repo", action="append", help="pick only from this repository, e.g. django/django (repeatable)")
    p.set_defaults(fn=cmd_import_swebench)

    p = sub.add_parser("models", help="list providers and whether their API key is set")
    p.add_argument("--providers-file", type=Path, help="extra OpenAI-compatible providers (attestor format)")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("report", help="score a results JSONL file")
    p.add_argument("results", type=Path, nargs="+", help="one or more results JSONL files (combined)")
    p.add_argument("--grades", type=Path, action="append",
                   help="graded CSV from `grade` (adds the diagnosis column; repeatable, one per run)")
    p.add_argument("--exclude", action="append", help="case id to leave out, e.g. one with a wrong label (repeatable)")
    p.add_argument("--out", type=Path, help="also write the report to this file")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("grade", help="write a CSV of correct rejections to grade for diagnosis by hand")
    p.add_argument("results", type=Path)
    p.add_argument("--out", type=Path, help="default: <results>.grades.csv")
    p.add_argument("--force", action="store_true", help="overwrite an existing grade sheet")
    p.set_defaults(fn=cmd_grade)

    p = sub.add_parser("figures", help="LaTeX tables and PDF/PNG figures for the paper")
    p.add_argument("results", type=Path, nargs="+", help="one or more results JSONL files (combined)")
    p.add_argument("--grades", type=Path, action="append", help="graded CSV from `grade` (repeatable, one per run)")
    p.add_argument("--exclude", action="append", help="case id to leave out, e.g. one with a wrong label (repeatable)")
    p.add_argument("--out", type=Path, help="default: reports/<run id>/")
    p.add_argument("--currency", default="", help="label for cost columns, e.g. USD")
    p.set_defaults(fn=cmd_figures)

    args = ap.parse_args(argv)
    load_dotenv()
    try:
        return args.fn(args)
    except CaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
