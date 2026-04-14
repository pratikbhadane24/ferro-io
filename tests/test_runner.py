"""Phase 8a tests: Runner class and run() signature."""
import asyncio
import pytest

import ferro_io


def test_run_rejects_non_coroutine():
    with pytest.raises(ValueError, match="coroutine"):
        ferro_io.run(42)


def test_run_rejects_loop_factory():
    async def main():
        return 1
    with pytest.raises(ValueError, match="loop_factory"):
        ferro_io.run(main(), loop_factory=lambda: None)


def test_run_accepts_debug_kwarg():
    async def main():
        return "ok"
    assert ferro_io.run(main(), debug=True) == "ok"


def test_runner_basic():
    async def work(x):
        await ferro_io.sleep(0.01)
        return x * 2

    with ferro_io.Runner() as runner:
        assert runner.run(work(3)) == 6
        assert runner.run(work(10)) == 20


def test_runner_get_loop():
    with ferro_io.Runner() as runner:
        loop = runner.get_loop()
        assert loop is not None


def test_runner_close_idempotent():
    runner = ferro_io.Runner()
    runner.close()
    runner.close()  # second call is a no-op


def test_runner_run_after_close_fails():
    runner = ferro_io.Runner()
    runner.close()

    async def main():
        return 1

    with pytest.raises(RuntimeError, match="closed"):
        runner.run(main())


def test_runner_rejects_loop_factory():
    with pytest.raises(ValueError, match="loop_factory"):
        ferro_io.Runner(loop_factory=lambda: None)


def test_runner_with_context():
    import contextvars
    var = contextvars.ContextVar("test_var", default="default")

    async def read_var():
        return var.get()

    ctx = contextvars.copy_context()
    ctx.run(var.set, "from_context")

    with ferro_io.Runner() as runner:
        result = runner.run(read_var(), context=ctx)
        assert result == "from_context"
