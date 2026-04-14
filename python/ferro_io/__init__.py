"""ferro_io — multi-core async runtime for Python, backed by Tokio.

Two modes:

1. **Library-level** (you control the imports):

       import ferro_io as asyncio

       async def main():
           await asyncio.gather(
               asyncio.sleep(0.1),
               asyncio.sleep(0.1),
           )

       asyncio.run(main())

2. **Process-wide** (third-party libraries also benefit):

       import ferro_io
       ferro_io.install()       # replaces sys.modules['asyncio']
       import aiofiles          # uses ferro_io under the hood

The top-level `run()` call drives the coroutine on a multi-thread Tokio
runtime rather than asyncio's single-threaded selector loop. Primitives
inside the coroutine (gather, Queue, Event, Lock, wait_for, timeout,
create_task, get_event_loop, run_in_executor, TaskGroup, streams,
subprocesses) pass through to asyncio because pyo3-async-runtimes already
exposes a real asyncio loop on top of Tokio — they get the parallelism for
free.

Anything not explicitly re-exported here falls through to the stdlib asyncio
module via `__getattr__`.

## Caveat

`from asyncio import sleep` at module top-level in a third-party library
captures a reference to stdlib `asyncio.sleep` *at import time*, before
`ferro_io.install()` has a chance to run. We cannot recover those references
without AST rewriting. To work around it, call `ferro_io.install()` as early
as possible (ideally as the first import in your entry point).
"""
from __future__ import annotations

import asyncio as _asyncio
import signal as _signal
import threading as _threading
from typing import Any

from ._ferro_io import AsyncRuntime, async_sleep, __version__

_async_sleep = async_sleep

__all__ = [
    # native runtime surface
    "AsyncRuntime",
    "async_sleep",
    "__version__",
    "Runner",
    "install",
    "uninstall",
    # asyncio surface (overrides)
    "run",
    "sleep",
    "gather",
    "wait_for",
    "timeout",
    "create_task",
    "ensure_future",
    "shield",
    "iscoroutine",
    "iscoroutinefunction",
    "get_event_loop",
    "new_event_loop",
    "run_in_executor",
    "Queue",
    "Event",
    "Lock",
    "Semaphore",
    "BoundedSemaphore",
    "Condition",
    "TimeoutError",
    "CancelledError",
]


# ---------------------------------------------------------------------------
# Runtime singleton
# ---------------------------------------------------------------------------

_rt: AsyncRuntime | None = None
_rt_lock = _threading.Lock()


def _runtime() -> AsyncRuntime:
    global _rt
    if _rt is None:
        with _rt_lock:
            if _rt is None:
                _rt = AsyncRuntime()
    return _rt


# ---------------------------------------------------------------------------
# SIGINT handling — match asyncio.Runner's contract.
# ---------------------------------------------------------------------------

class _SigintGuard:
    """Install a SIGINT handler that raises KeyboardInterrupt during run.

    Mirrors what asyncio.Runner does: only effective on the main thread, and
    restores the previous handler on exit.
    """

    def __init__(self) -> None:
        self._prev = None
        self._installed = False

    def __enter__(self) -> _SigintGuard:
        if _threading.current_thread() is _threading.main_thread():
            try:
                self._prev = _signal.signal(_signal.SIGINT, _signal.default_int_handler)
                self._installed = True
            except (ValueError, OSError):
                self._installed = False
        return self

    def __exit__(self, *exc) -> None:
        if self._installed:
            try:
                _signal.signal(_signal.SIGINT, self._prev)
            except (ValueError, OSError):
                pass
            self._installed = False


# ---------------------------------------------------------------------------
# run() / Runner
# ---------------------------------------------------------------------------

def run(coro, *, debug: bool | None = None, loop_factory=None) -> Any:
    """Drive `coro` to completion on the ferro_io Tokio runtime.

    Drop-in for `asyncio.run`. Accepts `debug` and `loop_factory` for source
    parity but `debug` is currently a no-op and `loop_factory` is rejected
    because ferro_io always uses its own runtime — passing one would silently
    bypass the Tokio fast path.
    """
    if loop_factory is not None:
        raise ValueError(
            "ferro_io.run does not support loop_factory; the ferro_io runtime "
            "is fixed. Use asyncio.run() directly if you need a custom loop."
        )
    if not _asyncio.iscoroutine(coro):
        raise ValueError(f"a coroutine was expected, got {coro!r}")
    with _SigintGuard():
        return _runtime().run_coroutine(coro)


class Runner:
    """Context manager equivalent of `asyncio.Runner`.

    Holds an `AsyncRuntime` (the process-global one) and lets you run multiple
    coroutines that share state. Use as a context manager:

        with ferro_io.Runner() as runner:
            result1 = runner.run(coro1())
            result2 = runner.run(coro2())
    """

    def __init__(self, *, debug: bool | None = None, loop_factory=None) -> None:
        if loop_factory is not None:
            raise ValueError(
                "ferro_io.Runner does not support loop_factory; "
                "use asyncio.Runner if you need a custom loop."
            )
        self._debug = debug
        self._closed = False
        self._sigint = _SigintGuard()
        self._entered = False

    def __enter__(self) -> Runner:
        self._sigint.__enter__()
        self._entered = True
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, coro, *, context=None) -> Any:
        if self._closed:
            raise RuntimeError("Runner is closed")
        if not _asyncio.iscoroutine(coro):
            raise ValueError(f"a coroutine was expected, got {coro!r}")
        if context is not None:
            return context.run(_runtime().run_coroutine, coro)
        return _runtime().run_coroutine(coro)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._entered:
            self._sigint.__exit__(None, None, None)
            self._entered = False

    def get_loop(self):
        """Return a stdlib asyncio loop object for compatibility callers.

        ferro_io's actual scheduler is Tokio, but consumers that introspect
        `runner.get_loop()` typically just want *some* event loop object.
        """
        try:
            return _asyncio.get_event_loop()
        except RuntimeError:
            return _asyncio.new_event_loop()


# ---------------------------------------------------------------------------
# Sleep — backed by tokio::time::sleep when used as `ferro_io.sleep`.
# ---------------------------------------------------------------------------

def sleep(delay: float, result: Any = None):
    """Async sleep backed by `tokio::time::sleep`.

    Matches `asyncio.sleep(delay, result=None)`.
    """
    if delay <= 0:
        # Stdlib asyncio.sleep(0) is a single-yield. Mirror that exactly so
        # code that uses sleep(0) as a yield-point keeps working.
        return _asyncio.sleep(0, result)
    if result is None:
        return _async_sleep(delay)

    async def _sleep_with_result():
        await _async_sleep(delay)
        return result

    return _sleep_with_result()


# ---------------------------------------------------------------------------
# Pass-through aliases — these run on the tokio-driven asyncio loop and
# behave identically to stdlib because that *is* the loop they're using.
# ---------------------------------------------------------------------------

gather = _asyncio.gather
wait_for = _asyncio.wait_for
timeout = _asyncio.timeout
create_task = _asyncio.create_task
ensure_future = _asyncio.ensure_future
shield = _asyncio.shield
iscoroutine = _asyncio.iscoroutine
iscoroutinefunction = _asyncio.iscoroutinefunction

Queue = _asyncio.Queue
Event = _asyncio.Event
Lock = _asyncio.Lock
Semaphore = _asyncio.Semaphore
BoundedSemaphore = _asyncio.BoundedSemaphore
Condition = _asyncio.Condition

TimeoutError = _asyncio.TimeoutError  # noqa: A001 — mirrors asyncio
CancelledError = _asyncio.CancelledError


def get_event_loop():
    """Return the currently running asyncio event loop (or a new one).

    Prefer `asyncio.get_running_loop()` inside coroutines.
    """
    try:
        return _asyncio.get_running_loop()
    except RuntimeError:
        return _asyncio.new_event_loop()


def new_event_loop():
    return _asyncio.new_event_loop()


async def run_in_executor(executor, func, *args):
    """Run `func(*args)` in a thread pool, awaitable from the coroutine."""
    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


# ---------------------------------------------------------------------------
# install / uninstall — re-exported from ferro_io._install for top-level access.
# ---------------------------------------------------------------------------

def install(*, rebind_existing: bool = True) -> None:
    """Replace `sys.modules['asyncio']` with ferro_io (process-wide drop-in)."""
    from . import _install
    _install.install(rebind_existing=rebind_existing)


def uninstall() -> None:
    """Restore the original `sys.modules['asyncio']`."""
    from . import _install
    _install.uninstall()


# ---------------------------------------------------------------------------
# Fall-through to stdlib asyncio for everything else.
# ---------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    """Resolve any unknown attribute against stdlib asyncio.

    Keeps `ferro_io.TaskGroup`, `ferro_io.StreamReader`, `ferro_io.exceptions.*`,
    `ferro_io.subprocess`, etc. all working without hand-listing every symbol.
    """
    return getattr(_asyncio, name)
