"""Phase 5 tests: pyo3-async-runtimes bridge."""
import asyncio
import time

import pytest
import ferro_io


async def test_async_sleep_awaitable():
    start = time.perf_counter()
    await ferro_io.async_sleep(0.05)
    elapsed = time.perf_counter() - start
    assert 0.04 <= elapsed < 0.25


async def test_async_sleep_gathered():
    start = time.perf_counter()
    await asyncio.gather(*[ferro_io.async_sleep(0.1) for _ in range(10)])
    elapsed = time.perf_counter() - start
    # Even through stdlib asyncio.gather, the sleeps run on Tokio and should
    # parallelize cleanly.
    assert elapsed < 0.35, f"expected ~0.1s, got {elapsed:.3f}s"


def test_run_coroutine_from_sync():
    rt = ferro_io.AsyncRuntime()

    async def work():
        await ferro_io.async_sleep(0.05)
        return 42

    result = rt.run_coroutine(work())
    assert result == 42
