"""Phase 8d: edge cases for the explicit overrides."""
import sys
import time

import pytest
import ferro_io


def test_sleep_zero_is_yield():
    async def main():
        # sleep(0) should be a fast yield, not a tokio timer.
        start = time.perf_counter()
        for _ in range(100):
            await ferro_io.sleep(0)
        return time.perf_counter() - start

    elapsed = ferro_io.run(main())
    assert elapsed < 0.05, f"sleep(0)×100 took {elapsed:.4f}s — too slow"


def test_sleep_negative_is_zero():
    """asyncio.sleep treats negative values like 0."""
    async def main():
        await ferro_io.sleep(-1)
        return "ok"

    assert ferro_io.run(main()) == "ok"


def test_sleep_with_result_kwarg():
    async def main():
        return await ferro_io.sleep(0.01, "done")

    assert ferro_io.run(main()) == "done"


def test_gather_return_exceptions_mixed():
    async def good():
        await ferro_io.sleep(0.01)
        return "ok"

    async def bad():
        await ferro_io.sleep(0.01)
        raise RuntimeError("intentional")

    async def main():
        return await ferro_io.gather(good(), bad(), good(), return_exceptions=True)

    results = ferro_io.run(main())
    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "ok"


def test_wait_for_no_timeout():
    async def work():
        await ferro_io.sleep(0.01)
        return "done"

    async def main():
        return await ferro_io.wait_for(work(), timeout=None)

    assert ferro_io.run(main()) == "done"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="asyncio.timeout requires Python 3.11+")
def test_timeout_none_context():
    async def main():
        async with ferro_io.timeout(None):
            await ferro_io.sleep(0.01)
        return "ok"

    assert ferro_io.run(main()) == "ok"


def test_create_task_with_name():
    async def worker():
        return 42

    async def main():
        task = ferro_io.create_task(worker(), name="my-task")
        assert task.get_name() == "my-task"
        return await task

    assert ferro_io.run(main()) == 42


def test_shield_completes():
    async def inner():
        await ferro_io.sleep(0.01)
        return "shielded"

    async def main():
        return await ferro_io.shield(inner())

    assert ferro_io.run(main()) == "shielded"


def test_ensure_future_from_coro():
    async def work():
        await ferro_io.sleep(0.01)
        return 99

    async def main():
        fut = ferro_io.ensure_future(work())
        return await fut

    assert ferro_io.run(main()) == 99
