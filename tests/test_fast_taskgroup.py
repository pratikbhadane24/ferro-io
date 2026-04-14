"""Regression tests for ferro_io.TaskGroup (FastTaskGroup) semantics.

The fast path skips add_done_callback for eagerly-completed, successful
tasks. These tests lock in that the slow path still handles exceptions,
cancellation, and mixed-completion workloads identically to stdlib.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

# Skip the entire module at collection time on Python < 3.11.
# Must come before `from ferro_io import TaskGroup` to avoid an AttributeError
# at import time (asyncio.TaskGroup doesn't exist before 3.11).
if sys.version_info < (3, 11):
    pytest.skip("asyncio.TaskGroup requires Python 3.11+", allow_module_level=True)

import ferro_io
from ferro_io import TaskGroup


# ---------- happy path ----------

def test_happy_path_eager_completes():
    async def ok():
        return 42

    async def main():
        results = []
        async with TaskGroup() as tg:
            tasks = [tg.create_task(ok()) for _ in range(100)]
        for t in tasks:
            results.append(t.result())
        return results

    assert ferro_io.run(main()) == [42] * 100


def test_happy_path_without_eager_factory():
    """TaskGroup still works when the eager factory is *not* active.

    ferro_io.run() auto-enables eager, so we test the no-eager path by
    explicitly resetting the factory inside the user coroutine.
    """
    async def ok():
        return "ok"

    async def main():
        asyncio.get_running_loop().set_task_factory(None)
        async with TaskGroup() as tg:
            tasks = [tg.create_task(ok()) for _ in range(10)]
        return [t.result() for t in tasks]

    assert ferro_io.run(main()) == ["ok"] * 10


# ---------- eager exception ----------

def test_eager_exception_propagates_as_exception_group():
    async def boom():
        raise ValueError("kaboom")

    async def main():
        async with TaskGroup() as tg:
            tg.create_task(boom())
            tg.create_task(boom())

    with pytest.raises(BaseExceptionGroup) as ei:  # noqa: F821
        ferro_io.run(main())
    assert all(isinstance(e, ValueError) for e in ei.value.exceptions)
    assert len(ei.value.exceptions) == 2


def test_eager_exception_cancels_siblings():
    """An eagerly-raised exception must still abort sibling tasks."""
    cancelled_flag = []

    async def slow():
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            cancelled_flag.append(True)
            raise

    async def boom():
        raise RuntimeError("eager boom")

    async def main():
        async with TaskGroup() as tg:
            tg.create_task(slow())
            tg.create_task(slow())
            tg.create_task(boom())

    with pytest.raises(BaseExceptionGroup) as ei:  # noqa: F821
        ferro_io.run(main())
    # boom raises, sibling slow tasks must have been cancelled
    assert any(isinstance(e, RuntimeError) for e in ei.value.exceptions)
    assert len(cancelled_flag) == 2


# ---------- mid-flight behaviour ----------

def test_mid_flight_exception_aborts_group():
    async def slow():
        await asyncio.sleep(1.0)
        return "never"

    async def boom():
        await asyncio.sleep(0.01)
        raise RuntimeError("mid-flight")

    async def main():
        async with TaskGroup() as tg:
            tg.create_task(slow())
            tg.create_task(boom())

    with pytest.raises(BaseExceptionGroup) as ei:  # noqa: F821
        ferro_io.run(main())
    assert any(isinstance(e, RuntimeError) for e in ei.value.exceptions)


def test_mixed_eager_and_async_tasks():
    async def fast():
        return 1

    async def slow():
        await asyncio.sleep(0.001)
        return 2

    async def main():
        async with TaskGroup() as tg:
            fast_tasks = [tg.create_task(fast()) for _ in range(10)]
            slow_tasks = [tg.create_task(slow()) for _ in range(5)]
        return [t.result() for t in fast_tasks + slow_tasks]

    assert ferro_io.run(main()) == [1] * 10 + [2] * 5


# ---------- invariants ----------

def test_create_task_rejects_outside_async_with():
    tg = TaskGroup()

    async def ok():
        return None

    # Not yet entered
    with pytest.raises(RuntimeError, match="has not been entered"):
        async def main():
            tg.create_task(ok())
        ferro_io.run(main())


def test_task_group_is_asyncio_subclass():
    assert issubclass(ferro_io.TaskGroup, asyncio.TaskGroup)
    assert ferro_io.TaskGroup is not asyncio.TaskGroup


# ---------- eager factory wiring ----------

@pytest.mark.skipif(sys.version_info < (3, 12), reason="eager_task_factory requires Python 3.12+")
def test_ferro_io_run_enables_eager_factory():
    """Any coroutine run via ferro_io.run() sees the eager factory active."""
    observed = []

    async def main():
        observed.append(asyncio.get_running_loop().get_task_factory())
        return None

    ferro_io.run(main())
    assert observed[0] is asyncio.eager_task_factory


@pytest.mark.skipif(sys.version_info < (3, 12), reason="eager_task_factory requires Python 3.12+")
def test_runner_enables_eager_factory():
    observed = []

    async def main():
        observed.append(asyncio.get_running_loop().get_task_factory())
        return None

    with ferro_io.Runner() as runner:
        runner.run(main())

    assert observed[0] is asyncio.eager_task_factory
