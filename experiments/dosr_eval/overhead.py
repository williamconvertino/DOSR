"""DOSR's own overhead vs change size: everything in a submission except the model.

Runs the real client pipeline (dosr_client.pipeline.submit) against the real attestor
app in-process, with the mock reviewer and the mock chain, on synthetic changes of
increasing size. Each step's time comes from the pipeline's own step timer, so this is
the same breakdown the CLI and GUI show. It also records the exact prompt the
attestor builds (chars/4 token estimate), which prices large changes without calling
a model.

Not measured here: network transfer (the attestor runs in-process), the review model
(replaced by the mock), a real EVM chain, and IPFS publishing.

Outputs: overhead.csv (one row per submission x step), overhead.md, overhead_steps.pdf/png.
"""

import csv
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from .figures import GRID, INK, INK_MUTED, SERIES_COLORS

ROOT = Path(__file__).resolve().parents[2]
SIZES = (10, 30, 100, 300, 1000, 3000)
BASE_FILES = 12
BASE_LINES_PER_FILE = 250
TEMPLATE = "security-sensitive-review-v2"

# Pipeline steps grouped for the chart; model review time is inside "await" (mock: ~0).
GROUPS = {
    "Client: commit, bundle, build PR + prompt": ("preflight", "commit", "parent", "bundle", "pr", "prompt",
                                                   "request", "validate"),
    "Attestor: upload, verify, review (mock), sign": ("upload", "await"),
    "Client: verify attestation, register on chain": ("verify", "register"),
}


def _function(i: int, body_lines: int) -> list[str]:
    """A plausible Python function of about body_lines + 3 lines."""
    out = [f"def handler_{i}(records, limit=100):", f'    """Process batch {i} of records."""', "    total = 0"]
    for j in range(max(body_lines - 3, 1)):
        out.append(f"    total += sum(r.get('v{j % 7}', 0) for r in records[:limit])  # step {j}")
    out.append("    return total")
    return out


def _module(name: str, lines: int, seed: int) -> str:
    out = [f'"""Module {name}."""', "", "import math", ""]
    i = 0
    while len(out) < lines:
        out += _function(seed * 1000 + i, 20) + [""]
        i += 1
    return "\n".join(out[:lines]) + "\n"


def make_seed(dir: Path) -> None:
    (dir / "app").mkdir(parents=True)
    (dir / "README.md").write_text("# bench\n\nSynthetic project for DOSR overhead benchmarks.\n")
    for k in range(BASE_FILES):
        (dir / "app" / f"mod_{k}.py").write_text(_module(f"mod_{k}", BASE_LINES_PER_FILE, k))


def apply_change(repo_path: Path, lines: int) -> int:
    """Append about `lines` new lines spread over up to BASE_FILES files (~250 lines per file)."""
    n_files = min(BASE_FILES, max(1, round(lines / 250)))
    per_file = max(1, lines // n_files)
    for k in range(n_files):
        p = repo_path / "app" / f"mod_{k}.py"
        new = []
        i = 0
        while len(new) < per_file:
            new += [""] + _function(900_000 + k * 1000 + i, min(30, per_file))
            i += 1
        p.write_text(p.read_text() + "\n".join(new[:per_file]) + "\n")
    return n_files


def run(sizes=SIZES, reps: int = 3, log=print) -> list[dict]:
    sys.path.insert(0, str(ROOT / "services" / "attestor"))
    from fastapi.testclient import TestClient

    from attestor.config import Settings
    from attestor.main import app as attestor_app, get_attestor
    from attestor.review import Attestor
    from dosr_chain import MockChain
    from dosr_client.attestor import AttestorClient
    from dosr_client.config import ClientProfile, DosrRepo
    from dosr_client.pipeline import SubmitOptions, submit
    from dosr_client.repo_ops import create_repository
    from dosr_protocol.policy import build_policy

    profile = ClientProfile(id="bench", name="Bench", email="bench@dosr.invalid")
    rows = []
    for size in sizes:
        for rep in range(reps):
            with tempfile.TemporaryDirectory(prefix="dosr-overhead-") as tmp:
                tmp = Path(tmp)
                chain = MockChain(tmp / "chain" / "state.json")
                settings = Settings(chain_state=chain.state_path, cache_dir=tmp / "attestor-cache")
                attestor_app.dependency_overrides[get_attestor] = lambda: Attestor(settings)
                try:
                    attestor = AttestorClient("http://testserver", http=TestClient(attestor_app))
                    seed = tmp / "seed"
                    make_seed(seed)
                    created = create_repository(
                        tmp / "repo", name="bench", policy=build_policy(
                            "standard", repo_name="bench", prompt_template=TEMPLATE,
                            max_changed_files=100, max_patch_bytes=5_000_000),
                        profile=profile, chain=chain, seed_dir=seed)
                    repo = DosrRepo(created["path"])
                    files = apply_change(repo.path, size)
                    sub = submit(repo, profile, attestor, chain, SubmitOptions(message=f"Add {size} lines"))
                    result = sub.to_dict()
                finally:
                    attestor_app.dependency_overrides.clear()
            if result["outcome"] != "approved":
                raise RuntimeError(f"size {size}: {result['outcome']}: {result['errors']}")
            pr = result["info"].get("pr", {})
            tokens = result["info"].get("pr_metrics", {}).get("total", {}).get("est_tokens")
            base = {"lines_target": size, "rep": rep, "files": files,
                    "lines": (pr.get("additions") or 0) + (pr.get("deletions") or 0),
                    "patch_bytes": pr.get("patch_bytes"), "prompt_est_tokens": tokens,
                    "bundle_bytes": (result["info"].get("bundle") or {}).get("size_bytes")}
            total = 0.0
            for s in result["steps"]:
                if s["status"] == "done" and s["duration_ms"] is not None:
                    rows.append({**base, "step": s["id"], "ms": s["duration_ms"]})
                    total += s["duration_ms"]
            rows.append({**base, "step": "TOTAL", "ms": total})
            log(f"  {size:>5} lines · rep {rep}: {total:7.0f} ms · ~{tokens:,} prompt tokens")
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    by = defaultdict(lambda: defaultdict(list))
    meta = {}
    for r in rows:
        by[r["lines_target"]][r["step"]].append(r["ms"])
        meta[r["lines_target"]] = r
    out = []
    for size in sorted(by):
        steps = by[size]
        entry = {"lines": meta[size]["lines"], "files": meta[size]["files"],
                 "patch_bytes": meta[size]["patch_bytes"], "prompt_est_tokens": meta[size]["prompt_est_tokens"],
                 "total_ms": statistics.median(steps["TOTAL"])}
        for g, ids in GROUPS.items():
            # Median per step, then summed: the group's typical cost.
            entry[g] = sum(statistics.median(steps[i]) for i in ids if steps.get(i))
        out.append(entry)
    return out


def table(summary: list[dict]) -> str:
    groups = list(GROUPS)
    md = ["## DOSR overhead by change size (mock reviewer, in-process attestor)", "",
          "| Lines changed | Files | Patch KB | Prompt tok (est) | " + " | ".join(groups) + " | Total (ms) |",
          "|---|---|---|---|" + "---|" * len(groups) + "---|"]
    for s in summary:
        md.append(f"| {s['lines']:,} | {s['files']} | {s['patch_bytes'] / 1024:.1f} | {s['prompt_est_tokens']:,} | "
                  + " | ".join(f"{s[g]:,.0f}" for g in groups) + f" | {s['total_ms']:,.0f} |")
    md += ["", "Median of the repetitions per step. Excludes network transfer, the review model, a real chain, "
           "and IPFS publishing."]
    return "\n".join(md) + "\n"


def chart(summary: list[dict], out: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "pdf.fonttype": 42,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    labels = [f"{s['lines']:,}" for s in summary]
    bottom = [0.0] * len(summary)
    for i, g in enumerate(GROUPS):
        vals = [s[g] / 1000 for s in summary]
        ax.bar(labels, vals, 0.6, bottom=bottom, color=SERIES_COLORS[i], edgecolor="white", linewidth=2,
               label=g, zorder=2)
        bottom = [b + v for b, v in zip(bottom, vals)]
    for x, total in enumerate(bottom):
        ax.text(x, total, f"{total:.2f}s", ha="center", va="bottom", fontsize=8, color=INK)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(INK_MUTED)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    ax.set_xlabel("Lines changed", fontsize=9, color=INK)
    ax.set_ylabel("Seconds (median)", fontsize=9, color=INK)
    ax.set_title("DOSR overhead per submission, excluding the review model", loc="left", fontsize=10.5,
                 color=INK, fontweight="bold", pad=14)
    ax.text(0, 1.02, "Real client pipeline + attestor, in-process; mock reviewer and mock chain.",
            transform=ax.transAxes, fontsize=8, color=INK_MUTED)
    ax.legend(frameon=False, fontsize=7.5, loc="upper left")
    paths = []
    for ext, kw in (("pdf", {}), ("png", {"dpi": 200})):
        p = out / f"overhead_steps.{ext}"
        fig.savefig(p, bbox_inches="tight", **kw)
        paths.append(p)
    plt.close(fig)
    return paths


def write_all(out: Path, sizes=SIZES, reps: int = 3, log=print) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    rows = run(sizes, reps, log)
    with (out / "overhead.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    summary = summarize(rows)
    (out / "overhead.md").write_text(table(summary), encoding="utf-8")
    return [out / "overhead.csv", out / "overhead.md"] + chart(summary, out)
