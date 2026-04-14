"""Heavyweight compatibility tests: asyncpg, SQLAlchemy async, FastAPI/Starlette.

These libraries do non-trivial things with the asyncio loop:

  - ``asyncpg``     — Cython extension with custom record types and strict
                      loop-identity assumptions.
  - ``SQLAlchemy``  — greenlet-based sync→async bridge that re-enters the
                      loop from worker threads.
  - ``FastAPI``     — ``anyio``-backed, heavy ``contextvars`` use through
                      Starlette middleware.

If ``ferro_io.install()`` is truly a drop-in replacement, these libraries should
work unmodified. Each test:

  1. Runs in a fresh subprocess so the install-before-import order is clean.
  2. Runs a matching ``stdlib`` control, so a bug in the test script itself
     does not look like a ferro_io bug.
  3. Is gated on ``FERRO_IO_INTEGRATION=1`` via the ``pg_dsn`` fixture.

Run locally with::

    docker compose -f tests/docker-compose.yml up -d
    FERRO_IO_INTEGRATION=1 pytest tests/test_heavyweights.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run(script: str, timeout: float = 90) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO, timeout=timeout,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"subprocess failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    payload = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    if not payload:
        pytest.fail(f"subprocess produced no JSON result:\nSTDOUT:\n{proc.stdout}")
    return json.loads(payload[-1])


def _needs(mod: str) -> None:
    """Skip the current test if ``mod`` is not importable in this interpreter."""
    pytest.importorskip(mod)


# =============================================================================
# asyncpg
# =============================================================================

_ASYNCPG_SCRIPT = '''
import json, sys
{install}
import asyncio
import asyncpg

DSN = {dsn!r}

async def main():
    conn = await asyncpg.connect(DSN)
    try:
        # 1. plain round-trip
        one = await conn.fetchval("SELECT 1")
        # 2. prepared + parameter binding
        stmt = await conn.prepare("SELECT $1::int + $2::int")
        sum_ = await stmt.fetchval(40, 2)
        # 3. custom record type via fetch()
        rows = await conn.fetch(
            "SELECT generate_series(1, 5) AS n, 'row-' || generate_series(1, 5) AS label"
        )
        labels = [r["label"] for r in rows]
        ns = [r["n"] for r in rows]
        # 4. transaction: commit path
        await conn.execute("DROP TABLE IF EXISTS ferro_io_tx")
        await conn.execute("CREATE TABLE ferro_io_tx (i int)")
        async with conn.transaction():
            await conn.execute("INSERT INTO ferro_io_tx VALUES (1), (2), (3)")
        committed = await conn.fetchval("SELECT count(*) FROM ferro_io_tx")
        # 5. transaction: rollback path
        try:
            async with conn.transaction():
                await conn.execute("INSERT INTO ferro_io_tx VALUES (99)")
                raise RuntimeError("rollback me")
        except RuntimeError:
            pass
        after_rollback = await conn.fetchval("SELECT count(*) FROM ferro_io_tx")
        await conn.execute("DROP TABLE ferro_io_tx")
    finally:
        await conn.close()

    # 6. pool under concurrent gather
    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=4)
    async def worker(i):
        async with pool.acquire() as c:
            return await c.fetchval("SELECT $1::int * 2", i)
    doubles = await asyncio.gather(*[worker(i) for i in range(20)])
    await pool.close()

    return {{
        "one": one,
        "sum": sum_,
        "labels": labels,
        "ns": ns,
        "committed": committed,
        "after_rollback": after_rollback,
        "doubles": doubles,
    }}

print(json.dumps(asyncio.run(main())))
'''


def _asyncpg_assertions(result: dict) -> None:
    assert result["one"] == 1
    assert result["sum"] == 42
    assert result["ns"] == [1, 2, 3, 4, 5]
    assert result["labels"] == [f"row-{i}" for i in range(1, 6)]
    assert result["committed"] == 3
    assert result["after_rollback"] == 3  # rollback left the committed rows alone
    assert result["doubles"] == [i * 2 for i in range(20)]


def test_asyncpg_under_ferro_io_install(pg_dsn):
    _needs("asyncpg")
    script = _ASYNCPG_SCRIPT.format(
        install="import ferro_io; ferro_io.install()",
        dsn=pg_dsn,
    )
    _asyncpg_assertions(_run(script))


def test_asyncpg_stdlib_control(pg_dsn):
    """Sanity: the same script works under stdlib asyncio. Guards against
    test-script bugs masquerading as ferro_io incompatibilities."""
    _needs("asyncpg")
    script = _ASYNCPG_SCRIPT.format(install="", dsn=pg_dsn)
    _asyncpg_assertions(_run(script))


# =============================================================================
# SQLAlchemy async (+ asyncpg driver)
# =============================================================================

_SQLALCHEMY_SCRIPT = '''
import json
{install}
import asyncio
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Item(Base):
    __tablename__ = "ferro_io_sa_items"
    id = Column(Integer, primary_key=True)
    label = Column(String, nullable=False)

DSN = {dsn!r}.replace("postgresql://", "postgresql+asyncpg://")

async def main():
    engine = create_async_engine(DSN, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with Session() as sess:
        sess.add_all([Item(id=i, label=f"item-{{i}}") for i in range(10)])
        await sess.commit()

    async with Session() as sess:
        result = await sess.execute(select(Item).order_by(Item.id))
        rows = [(it.id, it.label) for it in result.scalars().all()]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

    return {{"rows": rows}}

print(json.dumps(asyncio.run(main())))
'''


def _sqlalchemy_assertions(result: dict) -> None:
    assert result["rows"] == [[i, f"item-{i}"] for i in range(10)]


def test_sqlalchemy_async_under_ferro_io_install(pg_dsn):
    _needs("sqlalchemy")
    _needs("asyncpg")
    _needs("greenlet")
    script = _SQLALCHEMY_SCRIPT.format(
        install="import ferro_io; ferro_io.install()",
        dsn=pg_dsn,
    )
    _sqlalchemy_assertions(_run(script))


def test_sqlalchemy_async_stdlib_control(pg_dsn):
    _needs("sqlalchemy")
    _needs("asyncpg")
    _needs("greenlet")
    script = _SQLALCHEMY_SCRIPT.format(install="", dsn=pg_dsn)
    _sqlalchemy_assertions(_run(script))


# =============================================================================
# FastAPI / Starlette — no database required, runs in any env
# =============================================================================

_FASTAPI_SCRIPT = '''
import json
{install}
import asyncio
import contextvars
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
import anyio

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="none")

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request_id.set(request.headers.get("x-request-id", "unset"))
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        return response

app = FastAPI()
app.add_middleware(RequestIdMiddleware)

@app.get("/sync")
def sync_route():
    return {{"kind": "sync", "rid": request_id.get()}}

@app.get("/async")
async def async_route():
    await asyncio.sleep(0.001)
    return {{"kind": "async", "rid": request_id.get()}}

@app.get("/to-thread")
async def to_thread_route():
    # exercises anyio's thread pool
    def cpu():
        return sum(range(1000))
    v = await anyio.to_thread.run_sync(cpu)
    return {{"kind": "to_thread", "value": v}}

client = TestClient(app)
r1 = client.get("/sync", headers={{"x-request-id": "sync-1"}})
r2 = client.get("/async", headers={{"x-request-id": "async-1"}})
r3 = client.get("/to-thread")

print(json.dumps({{
    "sync": r1.json(),
    "async": r2.json(),
    "to_thread": r3.json(),
    "statuses": [r1.status_code, r2.status_code, r3.status_code],
}}))
'''


def _fastapi_assertions(result: dict) -> None:
    assert result["statuses"] == [200, 200, 200]
    assert result["sync"] == {"kind": "sync", "rid": "sync-1"}
    assert result["async"] == {"kind": "async", "rid": "async-1"}
    assert result["to_thread"] == {"kind": "to_thread", "value": sum(range(1000))}


@pytest.fixture(scope="session")
def fastapi_gate():
    # FastAPI/Starlette don't need Postgres, but still hide behind the integration
    # gate so casual `pytest tests/` runs stay fast and free of extra deps.
    import os
    if os.environ.get("FERRO_IO_INTEGRATION") != "1":
        pytest.skip("FERRO_IO_INTEGRATION=1 not set; skipping integration test")


def test_fastapi_under_ferro_io_install(fastapi_gate):
    _needs("fastapi")
    _needs("starlette")
    _needs("anyio")
    _needs("httpx")  # TestClient uses httpx internally
    script = _FASTAPI_SCRIPT.format(install="import ferro_io; ferro_io.install()")
    _fastapi_assertions(_run(script))


def test_fastapi_stdlib_control(fastapi_gate):
    _needs("fastapi")
    _needs("starlette")
    _needs("anyio")
    _needs("httpx")
    script = _FASTAPI_SCRIPT.format(install="")
    _fastapi_assertions(_run(script))
