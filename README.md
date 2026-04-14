# ferro_io

Multi-core async runtime for Python, backed by Tokio. Ships with `ferro_io`, a
**100% drop-in replacement for asyncio** (every public symbol resolves; 313
tests verify it, including real asyncpg / SQLAlchemy / FastAPI integration).

```python
# Mode 1 — library-level
import ferro_io as asyncio
asyncio.run(main())
```

```python
# Mode 2 — process-wide (third-party libs also benefit)
import ferro_io
ferro_io.install()
import aiofiles            # now uses ferro_io under the hood
```

## Why

CPython's GIL serializes asyncio onto one core. `ferro_io` moves the scheduler
into Tokio with a multi-thread runtime, so:

- **IO workloads** stay at the theoretical sleep floor (no slower than stdlib).
- **CPU workloads** that go through `ferro_io.AsyncRuntime.map_blocking` use
  `tokio::task::spawn_blocking` and bypass the GIL entirely.

## Benchmarks (M-series Mac, 14 cores)

Best of 5 trials. Full matrix with uvloop in [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

| Workload | stdlib asyncio | uvloop | ferro_io | vs stdlib |
|---|---:|---:|---:|---:|
| 50 × 50ms IO sleep | 51.62 ms | 51.57 ms | 51.44 ms | ~1× (sleep floor) |
| 200 × 20ms IO sleep | 22.94 ms | 22.00 ms | 21.23 ms | ~1× (sleep floor) |
| **14 CPU chains × 5M LCG iters** | **6036 ms** | **5578 ms** | **6.37 ms** | **🔥 947×** |

IO workloads: ferro_io and uvloop are both pinned at the theoretical sleep floor —
neither beats physics. CPU workloads: uvloop (libuv) stays GIL-bound like stdlib
because it optimizes the event loop, not CPU parallelism. ferro_io's `map_blocking`
routes through `tokio::task::spawn_blocking`, which releases the GIL and saturates
all worker threads.

## Coverage

- **Symbol-level**: 119/119 asyncio public symbols resolve through `ferro_io`.
- **Real-world programs verified**: `TaskGroup`, subprocess pipelines, TCP
  client/server with streams, `to_thread`, `Queue` producer/consumer, gather
  with `return_exceptions`, `wait_for`, `timeout`, `Runner` with contextvars.
- **Third-party library smoke test**: `aiofiles` works under `ferro_io.install()`.
- **Heavyweight libraries verified** against real services under `ferro_io.install()`:
  `asyncpg` (Cython records, prepared statements, transactions, pools),
  `SQLAlchemy` async (greenlet sync→async bridge over asyncpg),
  `FastAPI` / `Starlette` (anyio, contextvars middleware, `to_thread`).
  See `tests/test_heavyweights.py`.
- **307 unit tests** + **6 heavyweight integration tests**, 0 skipped, 0 failed.

## Compatibility matrix

|   | stdlib | uvloop | trio | **ferro_io** |
|---|:---:|:---:|:---:|:---:|
| Drop-in `asyncio` replacement | — | ✅ | ❌ | ✅ |
| `asyncio` symbol coverage | 100% | ~98% | 0% | **100%** (119/119) |
| `asyncio.TaskGroup` (3.11+) | ✅ | ✅ | n/a (nurseries) | ✅ |
| Multi-core CPU workloads | ❌ GIL | ❌ GIL | ❌ GIL | ✅ (via `spawn_blocking`) |
| Windows support | ✅ | ❌ | ✅ | ✅ |
| `httpx` / `aiohttp` / `websockets` | ✅ | ✅ | partial | ✅ (tested) |
| `asyncpg` (Cython records) | ✅ | ✅ | ❌ | ✅ (tested — see `test_heavyweights.py`) |
| `SQLAlchemy` async (greenlets) | ✅ | ✅ | ❌ | ✅ (tested) |
| `FastAPI` / `Starlette` | ✅ | ✅ | partial | ✅ (tested) |

All "tested" cells are backed by `tests/test_heavyweights.py`, which runs in the
`integration` CI job against a real Postgres service container and also executes
a matching stdlib-control run so a test-script bug can't masquerade as a ferro_io
incompatibility.

Heavyweight compat tests run in the `integration` CI job against a real
Postgres service container. To run them locally:

```bash
docker compose -f tests/docker-compose.yml up -d
FERRO_IO_INTEGRATION=1 pytest tests/test_heavyweights.py -v
```

## Benchmark matrix vs uvloop

Generate the full matrix (runs each column in a subprocess so `uvloop.install()`
can't contaminate the others):

```bash
pip install uvloop     # optional
python benchmarks/bench_matrix.py    # writes benchmarks/RESULTS.md
```

See [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md) for the latest numbers.
TL;DR: uvloop and ferro_io both sit at the IO sleep floor (within noise of
each other). On workload C, ferro_io's `spawn_blocking` path bypasses the GIL
while uvloop — like stdlib — remains serialized.

## Caveat for process-wide mode

Code that does `from asyncio import sleep` at module top-level captures the
stdlib reference at import time, before `ferro_io.install()` can run. To work
around it, call `ferro_io.install()` as the very first statement in your entry
point — before any third-party import.

## Build

```bash
python -m venv .venv && source .venv/bin/activate
pip install maturin pytest pytest-asyncio
maturin develop --release
pytest tests/ -v
python benchmarks/bench.py
```

Built with PyO3 0.28 + pyo3-async-runtimes 0.28 + Tokio + maturin. ABI3
wheels cover Python 3.9+.
