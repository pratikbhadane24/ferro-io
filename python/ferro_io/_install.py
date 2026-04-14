"""ferro_io._install — replace sys.modules['asyncio'] with ferro_io.

Usage:

    import ferro_io
    ferro_io.install()

    # or:
    from ferro_io import install
    install()

After installation, every subsequent `import asyncio` in any module returns
the ferro_io package instead of stdlib asyncio. Modules that have *already*
imported asyncio keep their cached reference unless `_rebind_existing_modules`
is also called (it runs by default from `install()`).

## Limitation

Code that does `from asyncio import sleep` at module import time captures
the stdlib reference before `install()` can run. We cannot recover that
without AST rewriting. To work around it, call `ferro_io.install()` as the
*very first* thing in your entry point — before any third-party library
that may pull in asyncio.
"""
from __future__ import annotations

import sys

import ferro_io as _ferro_io

_original_asyncio = sys.modules.get("asyncio")
_installed = False


def install(*, rebind_existing: bool = True) -> None:
    """Patch `sys.modules['asyncio']` to point at ferro_io. Idempotent."""
    global _installed, _original_asyncio
    if _installed:
        return
    _original_asyncio = sys.modules.get("asyncio")
    sys.modules["asyncio"] = _ferro_io
    if rebind_existing:
        _rebind_existing_modules()
    _installed = True


def uninstall() -> None:
    """Restore the original `sys.modules['asyncio']`. Idempotent."""
    global _installed
    if not _installed:
        return
    if _original_asyncio is not None:
        sys.modules["asyncio"] = _original_asyncio
    else:
        sys.modules.pop("asyncio", None)
    # Also rebind any modules whose `asyncio` attribute is currently ferro_io.
    _rebind_to(_original_asyncio)
    _installed = False


def is_installed() -> bool:
    return _installed


def _rebind_existing_modules() -> None:
    """Rebind any module whose `asyncio` attribute points at the original
    stdlib asyncio so it now points at ferro_io."""
    _rebind_to(_ferro_io)


def _rebind_to(target) -> None:
    """For every loaded module, if its `asyncio` attribute matches the
    *other* module, rebind it to `target`.

    Restricted to the literal attribute name `asyncio` — broad attribute
    sweeps risk corrupting unrelated state (we learned this the hard way
    when an over-eager rebind hit pytest internals)."""
    if target is None:
        return
    other = _original_asyncio if target is _ferro_io else _ferro_io
    if other is None:
        return
    for mod in list(sys.modules.values()):
        if mod is None or mod is _ferro_io or mod is target:
            continue
        try:
            current = getattr(mod, "asyncio", None)
        except Exception:
            continue
        if current is other:
            try:
                setattr(mod, "asyncio", target)
            except Exception:
                pass


# Note: this module does NOT auto-install on import. Call install() explicitly.
