"""Phase 8b: parametrized smoke test for every public asyncio symbol."""
import asyncio
import inspect

import pytest
import ferro_io


PUBLIC = sorted(n for n in dir(asyncio) if not n.startswith("_"))


@pytest.mark.parametrize("name", PUBLIC)
def test_symbol_resolves(name):
    """Every public asyncio symbol must resolve through ferro_io."""
    obj = getattr(ferro_io, name)
    assert obj is not None, f"ferro_io.{name} resolved to None"


@pytest.mark.parametrize("name", PUBLIC)
def test_symbol_matches_stdlib_for_passthrough(name):
    """For symbols we don't explicitly override, ferro_io.X must IS asyncio.X."""
    overridden = {
        # Functions and classes we replace with ferro_io versions.
        "run", "sleep", "Runner", "get_event_loop", "new_event_loop",
        "run_in_executor", "TaskGroup",
        # Names that don't belong to plain pass-through (ferro_io adds them).
        "AsyncRuntime", "install", "uninstall",
    }
    if name in overridden:
        return
    assert getattr(ferro_io, name) is getattr(asyncio, name), (
        f"ferro_io.{name} should be the same object as asyncio.{name}"
    )


# ---- Class instantiation sweep — run inside ferro_io.run() so primitives
# get a real running event loop, which is what they need.

INSTANTIABLE = [
    ("Queue", lambda: asyncio.Queue()),
    ("LifoQueue", lambda: asyncio.LifoQueue()),
    ("PriorityQueue", lambda: asyncio.PriorityQueue()),
    ("Event", lambda: asyncio.Event()),
    ("Lock", lambda: asyncio.Lock()),
    ("Semaphore", lambda: asyncio.Semaphore(1)),
    ("BoundedSemaphore", lambda: asyncio.BoundedSemaphore(1)),
    ("Condition", lambda: asyncio.Condition()),
    ("Barrier", lambda: asyncio.Barrier(1)) if hasattr(asyncio, "Barrier") else None,
    ("Future", lambda: asyncio.get_running_loop().create_future()),
]
INSTANTIABLE = [x for x in INSTANTIABLE if x is not None]


@pytest.mark.parametrize("name,factory", INSTANTIABLE)
def test_class_constructs_in_ferro_io_run(name, factory):
    async def main():
        obj = factory()
        assert obj is not None
        return name

    assert ferro_io.run(main()) == name


def test_taskgroup_via_ferro_io():
    """TaskGroup is a 3.11+ feature that resolves via fallthrough."""
    if not hasattr(ferro_io, "TaskGroup"):
        pytest.skip("TaskGroup requires Python 3.11+")

    async def main():
        results = []
        async with ferro_io.TaskGroup() as tg:
            for i in range(5):
                tg.create_task(_collect(results, i))
        return sorted(results)

    async def _collect(out, i):
        await ferro_io.sleep(0.001)
        out.append(i)

    assert ferro_io.run(main()) == [0, 1, 2, 3, 4]


def test_to_thread_via_ferro_io():
    """asyncio.to_thread should resolve and work."""
    if not hasattr(ferro_io, "to_thread"):
        pytest.skip("to_thread requires Python 3.9+")

    import time

    def blocking():
        time.sleep(0.01)
        return 42

    async def main():
        return await ferro_io.to_thread(blocking)

    assert ferro_io.run(main()) == 42
