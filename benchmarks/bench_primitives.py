"""Profile ferro_io's asyncio-primitive pass-throughs against stdlib and uvloop.

This harness isolates the per-op cost of individual asyncio primitives (Queue,
Lock, Event, create_task, TaskGroup) — no sleeps, no CPU work. It exists to
identify which primitive has the widest gap to the best-in-class implementation,
so a follow-up spec can target that primitive for a Tokio-backed port.

Three columns, each run in its own subprocess so uvloop.install() and
ferro_io.install() (both process-global side effects) can't contaminate
neighbors:

  1. stdlib  — CPython's built-in asyncio
  2. uvloop  — libuv-backed asyncio loop (skipped if not installed)
  3. ferro_io — ferro_io drop-in mode (today: pass-through to stdlib for all
                of these primitives; the measured gap is the headroom a
                Tokio-backed port would unlock)

Usage::

    python benchmarks/bench_primitives.py               # writes PRIMITIVES.md
    python benchmarks/bench_primitives.py --stdout      # print table only
    python benchmarks/bench_primitives.py --only stdlib # one column (internal)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
RESULTS = HERE / "PRIMITIVES.md"

COLUMNS = ["stdlib", "uvloop", "ferro_io"]


# ---------- workload counts (tuned for ~50–500ms per trial on M-series) ----------

QUEUE_SPSC_ITEMS = 100_000
QUEUE_MPMC_ITEMS = 100_000          # total across 4 producers
QUEUE_MPMC_WORKERS = 4
LOCK_CONTENTION_TASKS = 8
LOCK_CONTENTION_OPS = 10_000        # per task
EVENT_PINGPONG_ITERS = 20_000
TASK_SPAWN_N = 10_000
TASKGROUP_SPAWN_N = 10_000


# ---------- trial harness (lifted verbatim from bench.py:49-62) ----------

def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:8.2f} ms"


def _run_trials(label: str, fn, trials: int = 5) -> tuple[float, float, float]:
    samples: list[float] = []
    fn()  # warmup
    for _ in range(trials):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    samples.sort()
    mean = statistics.mean(samples)
    p50 = samples[len(samples) // 2]
    best = samples[0]
    print(f"  {label:<28s}  best={_fmt_ms(best)}  p50={_fmt_ms(p50)}  mean={_fmt_ms(mean)}")
    return best, p50, mean


# ---------- workloads ----------
#
# Each workload function accepts the already-imported asyncio module. We pass
# it in explicitly so the same source works under all three columns: stdlib
# leaves asyncio alone, uvloop swaps the loop policy, ferro_io rebinds
# sys.modules['asyncio'] via install(). All three then expose the same
# surface (`asyncio.Queue`, `asyncio.Lock`, etc.) and we measure whichever
# implementation the column wired up.

def make_queue_spsc(asyncio):
    async def main():
        q = asyncio.Queue()
        async def producer():
            for i in range(QUEUE_SPSC_ITEMS):
                q.put_nowait(i)
            q.put_nowait(None)
        async def consumer():
            while True:
                item = await q.get()
                if item is None:
                    return
        await asyncio.gather(producer(), consumer())
    return lambda: asyncio.run(main())


def make_queue_mpmc(asyncio):
    per_producer = QUEUE_MPMC_ITEMS // QUEUE_MPMC_WORKERS
    sentinel = object()

    async def main():
        q = asyncio.Queue()

        async def producer():
            for i in range(per_producer):
                await q.put(i)

        async def consumer():
            while True:
                item = await q.get()
                if item is sentinel:
                    return

        producers = [asyncio.create_task(producer()) for _ in range(QUEUE_MPMC_WORKERS)]
        consumers = [asyncio.create_task(consumer()) for _ in range(QUEUE_MPMC_WORKERS)]
        await asyncio.gather(*producers)
        for _ in range(QUEUE_MPMC_WORKERS):
            await q.put(sentinel)
        await asyncio.gather(*consumers)

    return lambda: asyncio.run(main())


def make_lock_contention(asyncio):
    async def main():
        lock = asyncio.Lock()
        counter = [0]

        async def worker():
            for _ in range(LOCK_CONTENTION_OPS):
                async with lock:
                    counter[0] += 1

        await asyncio.gather(*[worker() for _ in range(LOCK_CONTENTION_TASKS)])

    return lambda: asyncio.run(main())


def make_event_pingpong(asyncio):
    async def main():
        a = asyncio.Event()
        b = asyncio.Event()

        async def ping():
            for _ in range(EVENT_PINGPONG_ITERS):
                a.set()
                await b.wait()
                b.clear()

        async def pong():
            for _ in range(EVENT_PINGPONG_ITERS):
                await a.wait()
                a.clear()
                b.set()

        await asyncio.gather(ping(), pong())

    return lambda: asyncio.run(main())


def make_task_spawn(asyncio):
    async def noop():
        return None

    async def main():
        tasks = [asyncio.create_task(noop()) for _ in range(TASK_SPAWN_N)]
        await asyncio.gather(*tasks)

    return lambda: asyncio.run(main())


def make_taskgroup_spawn(asyncio):
    async def noop():
        return None

    async def main():
        async with asyncio.TaskGroup() as tg:
            for _ in range(TASKGROUP_SPAWN_N):
                tg.create_task(noop())

    return lambda: asyncio.run(main())


WORKLOADS = [
    ("queue_spsc",       "Queue SPSC (100k items)",           make_queue_spsc),
    ("queue_mpmc",       "Queue MPMC (4p/4c, 100k items)",    make_queue_mpmc),
    ("lock_contention",  "Lock contention (8×10k ops)",       make_lock_contention),
    ("event_pingpong",   "Event ping-pong (20k iters)",       make_event_pingpong),
    ("task_spawn",       "create_task spawn (10k no-ops)",    make_task_spawn),
    ("taskgroup_spawn",  "TaskGroup spawn (10k no-ops)",      make_taskgroup_spawn),
]


# ---------- column drivers (each runs inside its own subprocess) ----------

def _load_asyncio_for_column(column: str):
    """Return the asyncio module as seen by this column.

    Must be called as the very first thing in the child process, before any
    user code imports asyncio — the CLAUDE.md caveat about `from asyncio
    import X` capturing stdlib references applies here too.
    """
    if column == "stdlib":
        import asyncio
        return asyncio

    if column == "uvloop":
        import uvloop  # type: ignore
        uvloop.install()
        import asyncio
        return asyncio

    if column == "ferro_io":
        import ferro_io
        ferro_io.install()
        import asyncio  # resolves to ferro_io via sys.modules patch
        return asyncio

    raise ValueError(f"unknown column: {column}")


def run_column_in_process(column: str) -> dict:
    """Run every workload for one column, in the current (child) process."""
    asyncio = _load_asyncio_for_column(column)
    print(f"column: {column}  (asyncio module: {asyncio.__name__})")
    print()

    results: dict[str, float] = {}
    for key, label, factory in WORKLOADS:
        fn = factory(asyncio)
        best, _p50, _mean = _run_trials(label, fn, trials=5)
        results[key] = best
    print()
    return {"column": column, "available": True, "results": results}


# ---------- parent-side matrix assembly ----------

def _run_column_subprocess(column: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SELF), "--only", column, "--json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[warn] column {column} failed (rc={proc.returncode}):\n{proc.stderr}",
              file=sys.stderr)
        return {"column": column, "available": False, "error": proc.stderr.strip()}
    json_lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    if not json_lines:
        return {"column": column, "available": False, "error": "no json output"}
    # Forward the child's human-readable log so the user sees live progress.
    for ln in proc.stdout.splitlines():
        if not ln.startswith("{"):
            print(ln)
    return json.loads(json_lines[-1])


def _fmt_ms_opt(seconds: float | None) -> str:
    return f"{seconds * 1000:.2f} ms" if seconds is not None else "—"


def _speedup(baseline: float | None, value: float | None) -> str:
    if baseline is None or value is None or value == 0:
        return "—"
    return f"{baseline / value:.2f}×"


def build_table(raw: list[dict]) -> str:
    """Assemble the markdown matrix and the recommendation section."""
    by_column: dict[str, dict[str, float]] = {}
    for run in raw:
        if run.get("available"):
            by_column[run["column"]] = run["results"]

    lines: list[str] = []
    lines.append("# ferro_io primitive profiling\n")
    lines.append("Per-primitive micro-workloads. Each column runs in its own")
    lines.append("subprocess so `uvloop.install()` / `ferro_io.install()` can't")
    lines.append("contaminate neighbours. Best of 5 trials. Speedup is relative")
    lines.append("to stdlib asyncio.\n")
    lines.append("")
    lines.append("| Workload | stdlib | uvloop | ferro_io | uvloop vs stdlib | ferro_io vs stdlib |")
    lines.append("|---|---:|---:|---:|---:|---:|")

    stdlib = by_column.get("stdlib", {})
    uvloop = by_column.get("uvloop", {})
    ferro  = by_column.get("ferro_io", {})

    gaps: list[tuple[str, str, float, float]] = []
    # (key, label, ferro_vs_stdlib_ratio, best_available_vs_ferro_ratio)

    for key, label, _ in WORKLOADS:
        s = stdlib.get(key)
        u = uvloop.get(key)
        f = ferro.get(key)
        lines.append(
            f"| {label} | {_fmt_ms_opt(s)} | {_fmt_ms_opt(u)} | {_fmt_ms_opt(f)} "
            f"| {_speedup(s, u)} | {_speedup(s, f)} |"
        )
        if s is not None and f is not None:
            # Ratio of f/best_known. If f > best_known, there's headroom.
            best_known = min(v for v in (s, u, f) if v is not None)
            ratio = f / best_known if best_known > 0 else 1.0
            gaps.append((key, label, f / s if s > 0 else 1.0, ratio))

    lines.append("")
    lines.append("## Recommendation\n")

    if not gaps:
        lines.append("_No results available — is the harness broken?_")
        return "\n".join(lines) + "\n"

    # Rank by largest gap to best-known (biggest headroom a Tokio port could unlock).
    ranked = sorted(gaps, key=lambda r: r[3], reverse=True)
    top_key, top_label, _top_vs_stdlib, top_ratio = ranked[0]

    lines.append(
        f"**Target primitive: `{top_key}`** — {top_label}. "
        f"ferro_io is {top_ratio:.2f}× the best-known column on this workload, "
        "which is the widest gap in the matrix. The follow-up spec should port "
        "the backing primitive to a Tokio-backed implementation and re-run this "
        "harness to confirm the gap closes.\n"
    )

    lines.append("Full ranking (widest headroom first):\n")
    for i, (key, label, vs_stdlib, ratio) in enumerate(ranked, 1):
        lines.append(
            f"{i}. `{key}` — {label} — {ratio:.2f}× best-known, "
            f"{vs_stdlib:.2f}× stdlib"
        )

    return "\n".join(lines) + "\n"


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=COLUMNS,
        default=None,
        help="Run just one column (used internally for subprocess isolation)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single-line JSON summary at the end (for the parent process)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the matrix to stdout instead of writing PRIMITIVES.md",
    )
    args = parser.parse_args()

    # Child mode: run one column in this process, emit JSON, exit.
    if args.only is not None:
        if args.only == "uvloop":
            try:
                import uvloop  # type: ignore  # noqa: F401
            except ImportError:
                if args.json:
                    print(json.dumps({"column": "uvloop", "available": False,
                                      "error": "uvloop not installed"}))
                return
        result = run_column_in_process(args.only)
        if args.json:
            print(json.dumps(result))
        return

    # Parent mode: dispatch every column into a fresh subprocess and assemble.
    print(f"ferro_io primitive profiling (cpu_count={os.cpu_count()})")
    print()
    raw = [_run_column_subprocess(col) for col in COLUMNS]
    table = build_table(raw)
    if args.stdout:
        print(table)
    else:
        RESULTS.write_text(table)
        print(f"wrote {RESULTS}")


if __name__ == "__main__":
    main()
