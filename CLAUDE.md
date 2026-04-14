# ferro_io — agent rules

Mixed Rust/Python project: PyO3 + Tokio bindings (`ferro_io._ferro_io`) plus a
Python `ferro_io` drop-in shim for asyncio.

## Build loop

```bash
source .venv/bin/activate
cargo check                                                    # fast feedback
maturin develop --release                                      # rebuild the extension
pytest tests/ -q --ignore=tests/test_heavyweights.py           # fast unit loop
python benchmarks/bench.py                                     # spot-check perf
```

For the heavyweight integration loop (asyncpg / SQLAlchemy / FastAPI against a
real Postgres) and the uvloop benchmark matrix:

```bash
docker compose -f tests/docker-compose.yml up -d
FERRO_IO_INTEGRATION=1 pytest tests/test_heavyweights.py -v
python benchmarks/bench_matrix.py          # writes benchmarks/RESULTS.md
docker compose -f tests/docker-compose.yml down
```

## Non-negotiable rules

1. **Always release the GIL with `py.detach(...)` before `rt.block_on(...)`.**
   Holding the GIL across `block_on` deadlocks the moment any task tries to
   reacquire it via `Python::attach`. (PyO3 0.28 renamed the old
   `allow_threads` to `detach`.)
2. **Never hold `Python<'py>` or `Bound<'py, _>` across an `.await`.** Convert
   PyObjects into owned `Py<T>` or plain Rust types *before* entering the
   async block; reacquire the interpreter with `Python::attach` after it
   finishes.
3. **The Tokio runtime is process-global and leaked once.** `AsyncRuntime(...)`
   stores a `&'static Runtime` that lives in `GLOBAL_RT: OnceLock<...>`. The
   first call decides `worker_threads`; subsequent calls reuse it. Don't try
   to spin up a second runtime — `pyo3_async_runtimes::tokio::init_with_runtime`
   panics if called twice with conflicting state.
4. **Workload C is the headline benchmark.** `map_blocking` runs CPU work
   through `tokio::task::spawn_blocking`, which sidesteps the GIL entirely
   and saturates all worker threads. Don't regress it — it's where the
   ~950×-vs-stdlib, ~875×-vs-uvloop number lives (see Benchmark results
   below). uvloop optimizes the event loop, not CPU parallelism; on
   workload C it stays GIL-bound at ~5500ms while ferro_io lands at ~6ms.

## Stack

| Layer | Version | Notes |
|---|---|---|
| PyO3 | 0.28 | New `Bound<'py, T>` API; `detach`, not `allow_threads` |
| pyo3-async-runtimes | 0.28 | Use `tokio-runtime` feature; `into_future_with_locals` for sync→async bridges |
| Tokio | 1 (full features) | Multi-thread, all enabled |
| maturin | 1.13+ | mixed layout, `python-source = "python"` |
| ABI | abi3-py39 | one wheel covers Python 3.9–3.14 |

## asyncio compatibility (ferro_io shim) — verified

**Symbol coverage: 119/119** asyncio public symbols resolve through `ferro_io`.
**Test coverage: 307 unit tests + 6 heavyweight integration tests passing.**

The unit layer parametrizes every public symbol and covers real-world programs
(TaskGroup, subprocess, TCP streams, `to_thread`), edge cases, and the
`install()` round-trip.

The heavyweight layer (`tests/test_heavyweights.py`, gated behind
`FERRO_IO_INTEGRATION=1`) runs the libraries that historically break
asyncio replacements — each under `ferro_io.install()` with a matching stdlib
control so test-script bugs can't masquerade as ferro_io incompatibilities:

- **asyncpg** — Cython records, prepared statements, transactions (commit +
  rollback), `create_pool` under concurrent `gather`
- **SQLAlchemy async** — `create_async_engine`, `AsyncSession`, declarative
  insert + select over the asyncpg driver (exercises the greenlet sync→async
  bridge, historically the most fragile surface)
- **FastAPI / Starlette** — sync + async routes, `BaseHTTPMiddleware` reading
  `contextvars`, `anyio.to_thread.run_sync` as the backend

All pass unmodified. **No shim patches were needed** to support them — the
existing `__getattr__` fallthrough to stdlib already covers the event-loop
protocols these libraries rely on, because `ferro_io` owns the loop factory but
delegates the loop *object* to stdlib.

| API | Status | Backed by |
|---|---|---|
| `run(coro, *, debug, loop_factory)` | ✅ full | `AsyncRuntime.run_coroutine` → `run_until_complete` on Tokio. `loop_factory` rejected. |
| `Runner` | ✅ full | Holds the global `AsyncRuntime`; supports context manager, `run`, `close`, `get_loop`, `context=` |
| `sleep(delay, result=None)` | ✅ full | `tokio::time::sleep` via `future_into_py`. `sleep(0)` falls through to stdlib for proper yield semantics. |
| `gather` / `wait_for` / `timeout` | ✅ pass-through | stdlib running on tokio-driven loop |
| `create_task` / `ensure_future` / `shield` | ✅ pass-through | stdlib |
| `TaskGroup` (3.11+) | ✅ pass-through | stdlib (verified via real-world test) |
| `Queue` / `Event` / `Lock` / `Semaphore` / `Condition` / `Barrier` | ✅ pass-through | stdlib (works because we own the loop, not the primitives) |
| `to_thread` | ✅ pass-through | stdlib executor path |
| `run_in_executor` | ✅ pass-through | stdlib loop method |
| `create_subprocess_exec` / `create_subprocess_shell` | ✅ pass-through | stdlib subprocess (verified) |
| `start_server` / `open_connection` / streams | ✅ pass-through | stdlib (verified roundtrip) |
| Anything else (~99 more symbols) | ✅ fallthrough | `__getattr__` defers to `asyncio.*` |

## Drop-in modes

**Mode 1 — library-level** (you control the imports):
```python
import ferro_io as asyncio
asyncio.run(main())
```

**Mode 2 — process-wide** (third-party libs also benefit):
```python
import ferro_io
ferro_io.install()           # patches sys.modules['asyncio']
import aiofiles            # uses ferro_io under the hood
```

`install()` also rebinds the literal `asyncio` attribute on already-loaded
modules. The rebinder is **deliberately restricted to the attribute name
`asyncio`** — an over-eager broad sweep once corrupted pytest's path handling.
Don't widen it without strong reason.

**Caveat**: `from asyncio import sleep` at module top-level captures the
stdlib reference *before* `install()` runs. Workaround: call `ferro_io.install()`
as the very first thing in your entry point.

## Benchmark results (M-series Mac, 14 cores)

Generated by `python benchmarks/bench_matrix.py` — each column runs in its own
subprocess so `uvloop.install()` (a process-global side effect) can't
contaminate the others. Full output lives in `benchmarks/RESULTS.md`.

| Workload | stdlib asyncio | uvloop | ferro_io | vs stdlib |
|---|---:|---:|---:|---:|
| 50×50ms IO sleep | 51.62 ms | 51.57 ms | 51.44 ms | ~1× (floor) |
| 200×20ms IO sleep | 22.94 ms | 22.00 ms | 21.23 ms | ~1× (floor) |
| **14 CPU chains × 5M LCG iters** | **6036 ms** | **5578 ms** | **6.37 ms** | **947×** |

The CPU workload is the headline. IO workloads can't beat their own sleep
duration — they only need to not be slower than stdlib/uvloop, and on the
matrix they sit indistinguishably at the floor. uvloop's workload C uses
`ThreadPoolExecutor` via `run_in_executor` (what real uvloop users write); it
remains GIL-bound like stdlib because libuv cannot release the GIL for Python
bytecode. ferro_io's `map_blocking` routes through `spawn_blocking`, which
releases the GIL and is the only reason the 900×+ gap exists.

## Primitive benchmarks (`benchmarks/PRIMITIVES.md`)

Per-primitive micro-workloads exist at `benchmarks/bench_primitives.py` —
subprocess-isolated against `uvloop.install()` / `ferro_io.install()`. The
task-spawn workloads used to be at stdlib speed (ferro_io's `create_task` /
`TaskGroup` were pass-throughs to stdlib). They are now the fastest column:

| Primitive | stdlib | uvloop | ferro_io | ferro_io vs stdlib | ferro_io vs uvloop |
|---|---:|---:|---:|---:|---:|
| `create_task` spawn (10k) | 20.93 ms | 12.95 ms | **10.96 ms** | **1.91×** | **1.18×** |
| `TaskGroup` spawn (10k)   | 18.91 ms |  9.27 ms | **5.80 ms**  | **3.26×** | **1.60×** |

The wins come from two composed tricks, both in pure Python — no Rust changes:

1. **Eager task factory.** `ferro_io.run()` / `Runner.run()` wrap the user
   coroutine to set `asyncio.eager_task_factory` on the running loop before
   any user code runs. Coroutines that have no real await points complete
   synchronously inside `loop.create_task` and never touch the event loop at
   all — skipping the `call_soon` → `_run_once` → `Context.run` plumbing that
   cProfile shows is 80%+ of stdlib's task-spawn cost.
2. **`FastTaskGroup`** (`python/ferro_io/__init__.py`) subclasses
   `asyncio.TaskGroup` and inlines the fast path for eagerly-completed,
   non-exceptional tasks: no `_tasks.add`, no `add_done_callback`, no
   `future_add_to_awaited_by`. For any task that isn't already done, or that
   raised/was cancelled, it falls back to the exact stdlib bookkeeping so
   exception group propagation and mid-flight cancellation are unchanged.
   `ferro_io.TaskGroup` is wired to `FastTaskGroup`.

Queue / Lock / Event / Semaphore remain pass-throughs — profiling showed
zero gap between stdlib and uvloop on those primitives at benchmark scale,
so porting them is not justified by the data.
