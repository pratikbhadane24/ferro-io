"""Phase 2 + 3 tests: AsyncRuntime construction and parallel execution."""
import os
import time

import ferro_io


def test_construct_default():
    rt = ferro_io.AsyncRuntime()
    assert rt.worker_threads >= 1
    assert "AsyncRuntime" in repr(rt)


def test_construct_explicit_workers():
    rt = ferro_io.AsyncRuntime(worker_threads=4)
    # The *first* construction wins globally; later calls just mirror what they asked for.
    assert rt.worker_threads == 4


def test_run_concurrent_squares():
    rt = ferro_io.AsyncRuntime()
    out = rt.run_concurrent([1, 2, 3, 4, 5])
    assert out == [1, 4, 9, 16, 25]


def test_run_concurrent_empty():
    rt = ferro_io.AsyncRuntime()
    assert rt.run_concurrent([]) == []


def test_sleep_many_is_parallel():
    """The defining test: 10 tasks of 100ms must finish in ~100ms, not 1000ms."""
    rt = ferro_io.AsyncRuntime()
    # warmup
    rt.sleep_many(2, 10)
    start = time.perf_counter()
    out = rt.sleep_many(count=10, millis=100)
    elapsed = time.perf_counter() - start
    assert out == list(range(10))
    # Generous upper bound for CI jitter but still proves parallelism.
    assert elapsed < 0.35, f"expected ~0.1s, got {elapsed:.3f}s — tasks ran serially"
    assert elapsed >= 0.09, f"suspiciously fast ({elapsed:.3f}s) — did tasks actually sleep?"


def test_map_blocking_cpu_work():
    rt = ferro_io.AsyncRuntime()
    items = list(range(16))
    out = rt.map_blocking(items, iterations=1000)
    assert len(out) == len(items)
    # Deterministic hash: same input → same output.
    out2 = rt.map_blocking(items, iterations=1000)
    assert out == out2


def test_map_blocking_is_parallel_on_cpu():
    """spawn_blocking should saturate worker threads for CPU work."""
    if os.cpu_count() is None or os.cpu_count() < 2:
        return  # can't prove parallelism on single-core boxes
    rt = ferro_io.AsyncRuntime()
    # One heavy item to calibrate.
    iters = 2_000_000
    start = time.perf_counter()
    rt.map_blocking([1], iterations=iters)
    serial = time.perf_counter() - start

    items = list(range(os.cpu_count()))
    start = time.perf_counter()
    rt.map_blocking(items, iterations=iters)
    parallel = time.perf_counter() - start

    # N items should take significantly less than N×serial.
    assert parallel < serial * len(items) * 0.6, (
        f"parallel={parallel:.3f}s vs serial×N={serial * len(items):.3f}s "
        "— spawn_blocking isn't parallelizing"
    )
