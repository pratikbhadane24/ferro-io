"""Run each benchmark column in its own subprocess and emit a markdown matrix.

Why subprocesses? ``uvloop.install()`` is a process-global side effect. Running
it in the same interpreter as ferro_io contaminates the other columns. Forking
each column into its own process keeps them hermetic.

Usage::

    python benchmarks/bench_matrix.py           # writes benchmarks/RESULTS.md
    python benchmarks/bench_matrix.py --stdout  # print table to stdout only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE / "bench.py"
RESULTS = HERE / "RESULTS.md"

COLUMNS = ["stdlib", "uvloop", "ferro_io", "ferro_io"]


def run_column(col: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--only", col, "--json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[warn] column {col} failed (rc={proc.returncode}):\n{proc.stderr}",
              file=sys.stderr)
        return {"column": col, "available": False, "error": proc.stderr.strip()}
    # The final JSON line is the result; prior lines are the human-readable log.
    json_lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    if not json_lines:
        return {"column": col, "available": False, "error": "no json output"}
    return json.loads(json_lines[-1])


def _fmt_ms(seconds: float | None) -> str:
    return f"{seconds * 1000:.2f} ms" if seconds is not None else "—"


def _speedup(baseline: float | None, value: float | None) -> str:
    if baseline is None or value is None or value == 0:
        return "—"
    return f"{baseline / value:.1f}×"


def build_table(raw: list[dict]) -> str:
    # Collapse into {workload: {label: best_seconds}}
    by_workload: dict[str, dict[str, float]] = {}
    for run in raw:
        if not run.get("available"):
            continue
        for workload, rows in run.get("results", {}).items():
            by_workload.setdefault(workload, {}).update(rows)

    lines: list[str] = []
    lines.append("# ferro_io benchmark matrix\n")
    lines.append("Best of N trials per row. Speedup is relative to stdlib asyncio.\n")

    for workload in ["A", "B", "C"]:
        if workload not in by_workload:
            continue
        rows = by_workload[workload]
        baseline = rows.get("stdlib asyncio") or rows.get("asyncio (no parallelism)")
        title = {
            "A": "Workload A — 50 × 50ms IO sleep",
            "B": "Workload B — 200 × 20ms IO sleep",
            "C": "Workload C — CPU-bound LCG chains (headline)",
        }[workload]
        lines.append(f"\n## {title}\n")
        lines.append("| Implementation | Best | vs stdlib |")
        lines.append("|---|---|---|")
        for label, best in rows.items():
            lines.append(f"| {label} | {_fmt_ms(best)} | {_speedup(baseline, best)} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    raw = [run_column(c) for c in COLUMNS]
    table = build_table(raw)
    if args.stdout:
        print(table)
    else:
        RESULTS.write_text(table)
        print(f"wrote {RESULTS}")


if __name__ == "__main__":
    main()
