"""Phase 5b tests: import-swap compatibility with asyncio."""
import time

import ferro_io as asyncio  # the one-line swap


def test_run_simple():
    async def main():
        return 7

    assert asyncio.run(main()) == 7


def test_run_with_sleep():
    async def main():
        await asyncio.sleep(0.02)
        return "done"

    assert asyncio.run(main()) == "done"


def test_gather_parallelism():
    """The point of the whole exercise: parallel sleeps must actually parallelize."""
    async def main():
        await asyncio.gather(
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
        )

    start = time.perf_counter()
    asyncio.run(main())
    elapsed = time.perf_counter() - start
    assert elapsed < 0.35, f"expected ~0.1s, got {elapsed:.3f}s — not parallel"


def test_sleep_with_result():
    async def main():
        return await asyncio.sleep(0.01, result="hello")

    assert asyncio.run(main()) == "hello"


def test_gather_return_values():
    async def work(i):
        await asyncio.sleep(0.01)
        return i * i

    async def main():
        return await asyncio.gather(*[work(i) for i in range(5)])

    assert asyncio.run(main()) == [0, 1, 4, 9, 16]


def test_wait_for_timeout():
    async def slow():
        await asyncio.sleep(1.0)
        return "never"

    async def main():
        try:
            await asyncio.wait_for(slow(), timeout=0.05)
        except (asyncio.TimeoutError, TimeoutError):
            return "timed out"

    assert asyncio.run(main()) == "timed out"


def test_create_task():
    async def worker():
        await asyncio.sleep(0.01)
        return 99

    async def main():
        t = asyncio.create_task(worker())
        return await t

    assert asyncio.run(main()) == 99


def test_queue_fallthrough():
    """asyncio.Queue works transparently because the loop underneath is a real asyncio loop."""
    async def producer(q):
        for i in range(3):
            await q.put(i)
        await q.put(None)

    async def consumer(q):
        out = []
        while True:
            item = await q.get()
            if item is None:
                return out
            out.append(item)

    async def main():
        q = asyncio.Queue()
        _, items = await asyncio.gather(producer(q), consumer(q))
        return items

    assert asyncio.run(main()) == [0, 1, 2]


def test_getattr_fallthrough():
    import asyncio as stdlib_asyncio
    # Symbols we don't override still resolve via __getattr__.
    assert asyncio.iscoroutinefunction(test_getattr_fallthrough.__wrapped__
                                       if hasattr(test_getattr_fallthrough, "__wrapped__")
                                       else test_getattr_fallthrough) is False
    assert asyncio.CancelledError is stdlib_asyncio.CancelledError
