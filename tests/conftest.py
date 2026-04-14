"""Shared pytest fixtures for ferro_io tests.

The heavyweight integration tests (asyncpg / SQLAlchemy / FastAPI against a
real Postgres) live behind the ``FERRO_IO_INTEGRATION=1`` gate so the default
``pytest tests/ -q`` loop stays fast for local dev.
"""
from __future__ import annotations

import os
import socket

import pytest


def _integration_enabled() -> bool:
    return os.environ.get("FERRO_IO_INTEGRATION") == "1"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    if not _integration_enabled():
        pytest.skip("FERRO_IO_INTEGRATION=1 not set; skipping integration test")
    dsn = os.environ.get(
        "FERRO_IO_PG_DSN",
        "postgresql://ferro_io:ferro_io@localhost:55432/ferro_io",
    )
    host, port = _parse_host_port(dsn)
    with socket.socket() as s:
        s.settimeout(2)
        try:
            s.connect((host, port))
        except OSError as e:
            pytest.skip(f"Postgres not reachable at {host}:{port} ({e})")
    return dsn


def _parse_host_port(dsn: str) -> tuple[str, int]:
    # postgresql://user:pw@host:port/db  — minimal parse, good enough for the fixture
    after_at = dsn.rsplit("@", 1)[-1]
    host_port = after_at.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.split(":", 1)
        return host, int(port)
    return host_port, 5432
