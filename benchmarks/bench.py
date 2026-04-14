"""Benchmark ferro_io against stdlib asyncio, ThreadPoolExecutor, and uvloop.

Columns:
  1. stdlib asyncio            — single-threaded selector loop
  2. ThreadPoolExecutor        — OS threads with GIL contention
  3. uvloop                    — libuv-backed asyncio loop (if installed, non-Windows)
  4. ferro_io.AsyncRuntime     — direct Rust API, Tokio multi-thread
  5. ferro_io drop-in            — same as col 1, only the import line changes

Workloads:
  A. IO-bound parallel sleep   — 50 tasks × 50ms simulated IO
  B. IO-bound higher fanout    — 200 tasks × 20ms simulated IO
  C. CPU-bound via spawn_blocking — 16 tasks × ~5M LCG iterations

This file can be driven two ways:

  $ python benchmarks/bench.py               # runs all columns in one process
  $ python benchmarks/bench.py --only uvloop # runs just the uvloop column
                                             # (used by bench_matrix.py to
                                             # keep uvloop.install() contained)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import ferro_io
import ferro_io

try:
    import uvloop  # type: ignore
    HAS_UVLOOP = True
except ImportError:
    uvloop = None  # type: ignore
    HAS_UVLOOP = False


# ---------- helpers ----------

def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:8.2f} ms"


def _run_trials(label: str, fn, trials: int = 5) -> tuple[float, float, float]:
    samples = []
    # warmup
    fn()
    for _ in range(trials):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    samples.sort()
    mean = statistics.mean(samples)
    p50 = samples[len(samples) // 2]
    best = samples[0]
    print(f"  {label:<32s}  best={_fmt_ms(best)}  p50={_fmt_ms(p50)}  mean={_fmt_ms(mean)}")
    return best, p50, mean


# ---------- workload A: 50 × 50ms sleep ----------

A_COUNT = 50
A_MS = 50


def a_asyncio():
    async def main():
        await asyncio.gather(*[asyncio.sleep(A_MS / 1000) for _ in range(A_COUNT)])
    asyncio.run(main())


def a_threadpool():
    with ThreadPoolExecutor(max_workers=A_COUNT) as pool:
        list(pool.map(lambda _: time.sleep(A_MS / 1000), range(A_COUNT)))


_rt = ferro_io.AsyncRuntime()


def a_ferro_io_direct():
    _rt.sleep_many(A_COUNT, A_MS)


def a_ferro_io_dropin():
    async def main():
        await ferro_io.gather(*[ferro_io.sleep(A_MS / 1000) for _ in range(A_COUNT)])
    ferro_io.run(main())


def a_uvloop():
    # Must be called in a subprocess (bench_matrix.py) — uvloop.install() is
    # a global side-effect that persists for the life of the interpreter.
    assert HAS_UVLOOP, "uvloop not installed"
    uvloop.install()
    async def main():
        await asyncio.gather(*[asyncio.sleep(A_MS / 1000) for _ in range(A_COUNT)])
    asyncio.run(main())


# ---------- workload B: 200 × 20ms ----------

B_COUNT = 200
B_MS = 20


def b_asyncio():
    async def main():
        await asyncio.gather(*[asyncio.sleep(B_MS / 1000) for _ in range(B_COUNT)])
    asyncio.run(main())


def b_threadpool():
    with ThreadPoolExecutor(max_workers=min(B_COUNT, 64)) as pool:
        list(pool.map(lambda _: time.sleep(B_MS / 1000), range(B_COUNT)))


def b_ferro_io_direct():
    _rt.sleep_many(B_COUNT, B_MS)


def b_ferro_io_dropin():
    async def main():
        await ferro_io.gather(*[ferro_io.sleep(B_MS / 1000) for _ in range(B_COUNT)])
    ferro_io.run(main())


def b_uvloop():
    assert HAS_UVLOOP, "uvloop not installed"
    uvloop.install()
    async def main():
        await asyncio.gather(*[asyncio.sleep(B_MS / 1000) for _ in range(B_COUNT)])
    asyncio.run(main())


# ---------- workload C: CPU-bound spawn_blocking ----------

C_ITEMS = list(range(os.cpu_count() or 4))
C_ITERS = 5_000_000


def c_asyncio_synchronous():
    """asyncio has no good answer for CPU work — this is basically serial."""
    def work(v):
        x = v
        for _ in range(C_ITERS):
            x = (x * 2862933555777941757 + 3037000493) & 0xFFFFFFFFFFFFFFFF
        return x
    async def main():
        return [work(v) for v in C_ITEMS]
    asyncio.run(main())


def c_threadpool():
    def work(v):
        x = v
        for _ in range(C_ITERS):
            x = (x * 2862933555777941757 + 3037000493) & 0xFFFFFFFFFFFFFFFF
        return x
    with ThreadPoolExecutor(max_workers=len(C_ITEMS)) as pool:
        list(pool.map(work, C_ITEMS))


def c_ferro_io_direct():
    _rt.map_blocking(C_ITEMS, C_ITERS)


def _lcg_work(v: int) -> int:
    x = v
    for _ in range(C_ITERS):
        x = (x * 2862933555777941757 + 3037000493) & 0xFFFFFFFFFFFFFFFF
    return x


def c_uvloop():
    """Workload C under uvloop — ThreadPoolExecutor (real-world uvloop usage).

    uvloop optimizes the event loop, not CPU parallelism. Production uvloop code
    offloads CPU work via ``loop.run_in_executor`` with a ThreadPool, which remains
    GIL-bound. This mirrors what users actually write.
    """
    assert HAS_UVLOOP, "uvloop not installed"
    uvloop.install()

    async def main():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=len(C_ITEMS)) as pool:
            await asyncio.gather(*[
                loop.run_in_executor(pool, _lcg_work, v) for v in C_ITEMS
            ])

    asyncio.run(main())


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["stdlib", "ferro_io", "ferro_io", "uvloop", "all"],
        default="all",
        help="Run just one column (used by bench_matrix.py for subprocess isolation)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single-line JSON summary (for bench_matrix.py)",
    )
    args = parser.parse_args()

    if args.only == "uvloop" and not HAS_UVLOOP:
        if args.json:
            import json as _json
            print(_json.dumps({"column": "uvloop", "available": False}))
            return
        print("uvloop not installed; skipping")
        return

    print(f"ferro_io benchmark (cpu_count={os.cpu_count()}, workers={_rt.worker_threads})")
    print()

    results: dict[str, dict[str, float]] = {}

    def run_col(label: str, fn, workload: str, trials: int = 5) -> None:
        best = _run_trials(label, fn, trials=trials)[0]
        results.setdefault(workload, {})[label] = best

    want = lambda col: args.only in ("all", col)  # noqa: E731

    print(f"Workload A — IO-bound: {A_COUNT} tasks × {A_MS}ms simulated sleep")
    if want("stdlib"):
        run_col("stdlib asyncio", a_asyncio, "A")
        run_col("ThreadPoolExecutor", a_threadpool, "A")
    if want("uvloop") and HAS_UVLOOP and sys.platform != "win32":
        run_col("uvloop", a_uvloop, "A")
    if want("ferro_io"):
        run_col("ferro_io.AsyncRuntime (direct)", a_ferro_io_direct, "A")
    if want("ferro_io"):
        run_col("ferro_io drop-in (import swap)", a_ferro_io_dropin, "A")
    print()

    print(f"Workload B — higher fanout: {B_COUNT} tasks × {B_MS}ms sleep")
    if want("stdlib"):
        run_col("stdlib asyncio", b_asyncio, "B")
        run_col("ThreadPoolExecutor", b_threadpool, "B")
    if want("uvloop") and HAS_UVLOOP and sys.platform != "win32":
        run_col("uvloop", b_uvloop, "B")
    if want("ferro_io"):
        run_col("ferro_io.AsyncRuntime (direct)", b_ferro_io_direct, "B")
    if want("ferro_io"):
        run_col("ferro_io drop-in (import swap)", b_ferro_io_dropin, "B")
    print()

    print(f"Workload C — CPU-bound: {len(C_ITEMS)} LCG chains × {C_ITERS:,} iters")
    if want("stdlib"):
        run_col("asyncio (no parallelism)", c_asyncio_synchronous, "C", trials=3)
        run_col("ThreadPoolExecutor (GIL!)", c_threadpool, "C", trials=3)
    if want("uvloop") and HAS_UVLOOP and sys.platform != "win32":
        run_col("uvloop", c_uvloop, "C", trials=3)
    if want("ferro_io"):
        run_col("ferro_io.map_blocking", c_ferro_io_direct, "C", trials=3)
    print()

    if args.json:
        import json as _json
        print(_json.dumps({"column": args.only, "available": True, "results": results}))


if __name__ == "__main__":
    main()
