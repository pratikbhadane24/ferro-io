"""Phase 9b: ferro_io.install / sys.modules monkey-patch."""
import sys

import pytest


@pytest.fixture(autouse=True)
def restore_asyncio():
    """Always uninstall after each test so the suite isn't polluted."""
    yield
    try:
        import ferro_io
        ferro_io.uninstall()
    except Exception:
        pass


def test_install_replaces_sys_modules():
    import ferro_io
    ferro_io.install()
    import asyncio
    assert asyncio is ferro_io
    ferro_io.uninstall()


def test_uninstall_restores_stdlib():
    import ferro_io
    # Capture the real stdlib reference first.
    ferro_io.uninstall()  # ensure clean
    real_asyncio = sys.modules.get("asyncio")
    if real_asyncio is ferro_io:
        # In case a previous test left it dirty.
        del sys.modules["asyncio"]
        import asyncio as real_asyncio  # noqa: F401
        real_asyncio = sys.modules["asyncio"]

    ferro_io.install()
    assert sys.modules["asyncio"] is ferro_io
    ferro_io.uninstall()
    assert sys.modules.get("asyncio") is not ferro_io


def test_late_import_sees_ferro_io():
    import importlib
    import ferro_io
    ferro_io.install()
    asyncio_again = importlib.import_module("asyncio")
    assert asyncio_again is ferro_io


def test_install_idempotent():
    import ferro_io
    ferro_io.install()
    ferro_io.install()  # second call is a no-op
    assert sys.modules["asyncio"] is ferro_io


def test_rebind_existing_module():
    """A module that holds a reference to the original asyncio gets rebound."""
    import types
    import ferro_io

    ferro_io.uninstall()
    import asyncio as real_asyncio

    fixture = types.ModuleType("ferro_io_test_fixture_mod")
    fixture.asyncio = real_asyncio
    sys.modules["ferro_io_test_fixture_mod"] = fixture

    try:
        ferro_io.install()  # rebind_existing=True by default
        assert fixture.asyncio is ferro_io
        ferro_io.uninstall()
        # After uninstall, fixture.asyncio is rebound back.
        assert fixture.asyncio is real_asyncio
    finally:
        del sys.modules["ferro_io_test_fixture_mod"]


def test_run_through_stdlib_name_after_install():
    """After install, calling asyncio.run() should drive on ferro_io."""
    import ferro_io
    ferro_io.install()
    import asyncio  # this is ferro_io now

    async def main():
        return await asyncio.sleep(0.01, "ok")

    assert asyncio.run(main()) == "ok"


# ---- Phase 9c — optional aiofiles smoke test ----

aiofiles = pytest.importorskip("aiofiles")


def test_aiofiles_under_install():
    import ferro_io
    ferro_io.install()

    async def main():
        async with aiofiles.open(__file__, "r") as f:
            content = await f.read()
        return len(content)

    n = ferro_io.run(main())
    assert n > 0
