# ferro_io

> ## Status: Discontinued (April 2026)
>
> **This project is no longer under active development.**
>
> ferro_io was an experiment in shipping a Tokio-backed drop-in asyncio
> replacement for Python. Empirical benchmarking against real-world Python
> workloads showed that the project, as framed, does not solve the problem it
> claimed to solve. The code is left public as a reference implementation of
> PyO3 0.28 + pyo3-async-runtimes 0.28 + Tokio bridging, but **do not use it in
> production** and do not expect updates.
>
> See [Findings](#findings) below for the full write-up of what was tested and
> why the premise does not hold on current CPython.
>
> If you want to continue the architectural bet (multi-thread async runtime
> positioned for PEP 703 free-threaded Python), fork freely — the MIT license
> stands.

---

## Findings

### Original claim

> "CPython's GIL serializes asyncio onto one core. `ferro_io` moves the
> scheduler into Tokio with a multi-thread runtime, so CPU workloads bypass
> the GIL and IO workloads stay at the theoretical sleep floor."

Headline benchmark reported **947× vs stdlib** on a "14 CPU chains × 5M LCG
iters" workload.

### What the benchmark actually measured

The 947× number comes from `AsyncRuntime.map_blocking`, whose signature is
fixed to `(items: Vec<u64>, iterations: u64) -> Vec<u64>` (see `src/lib.rs`
lines 118–146). The "Python" side of the comparison is a Python LCG loop; the
"ferro_io" side is a Rust LCG loop run via `tokio::task::spawn_blocking`.

This measures **Rust arithmetic vs Python arithmetic**. It does not measure
runtime-vs-runtime. Any Rust extension beats Python arithmetic by a similar
factor; this is a property of Rust, not of ferro_io.

Users cannot plug their own Rust kernels into `map_blocking` — the API is
locked to `u64` in, `u64` out. So the headline win is not accessible from
arbitrary Python code.

### Real-world benchmark (bcrypt / Pillow / ReportLab / WeasyPrint)

Benchmarked on a real application's `run_in_cpu_thread` hot paths: bcrypt
password hashing, Pillow image resize, ReportLab PDF generation, WeasyPrint
HTML→PDF. Each run compared stdlib `asyncio` default executor against
`ferro_io.install()` + `AsyncRuntime(worker_threads=8)`, in separate processes
so `install()`'s global `sys.modules` patch could not contaminate the
baseline. Serial runs were best-of-5 after a warmup; concurrent runs used
`asyncio.gather` at N = 20, 100, 300, 1000.

| Workload | Serial latency (ferro_io vs stdlib) | Concurrent throughput (ferro_io vs stdlib) |
|---|---|---|
| bcrypt cost-10 | tied (< 1 ms) | tied across all N |
| Pillow resize 2000→800 | tied | tied after warmup (cold-pool artifact on first batch) |
| ReportLab invoice PDF | tied | tied |
| WeasyPrint MITC PDF | tied | tied; both runtimes segfault stochastically at N ≥ 100 (Pango/cairo not thread-safe — a C-stack issue no Python runtime can fix) |

**No measurable throughput or latency win.** The one gap that looked like a
ferro_io win on the first Pillow batch (~65% faster) was pre-warmed-pool
behavior and disappears on subsequent runs; a `prestart_executor_threads`
call on stdlib closes it for free.

### Why the premise does not hold on current CPython

1. **The GIL is a CPython interpreter invariant.** Any library that runs
   Python bytecode in threads hits it. Moving the scheduler to Rust/Tokio
   does not remove this constraint — the scheduler is microseconds; the
   bytecode is milliseconds. Wrong layer of attack.
2. **C extensions that release the GIL already parallelize under stdlib's
   `ThreadPoolExecutor`.** bcrypt, Pillow (libjpeg), zstd, orjson all drop
   the GIL in their C code. A different scheduler doesn't help them — the
   thread pool is the parallelism mechanism, not the loop.
3. **Pure-Python CPU work stays GIL-bound regardless of runtime.** ReportLab
   is the clean example: Python bytecode, GIL held throughout, identical
   timings on stdlib and ferro_io.
4. **IO workloads are wall-clock dominated.** A 50 ms network RTT is 50 ms
   under any runtime. ferro_io, uvloop, and stdlib all sit at the sleep
   floor because they cannot beat physics.
5. **`map_blocking`'s spawn_blocking trick only wins when the closure
   contains pure Rust code.** The moment you call back into Python inside
   `spawn_blocking`, the GIL re-acquires and the gain vanishes. ferro_io
   exposes no generic mechanism for users to write that pure-Rust closure.

The architectural bet (Tokio multi-thread runtime scheduling Python)
**would** become meaningful on PEP 703 free-threaded Python (`python3.13t`,
`python3.14t`), where the GIL is genuinely removable. ferro_io was not
audited or tested against free-threaded builds during its active period.
Anyone forking this work should start there.

### What was real

- **PyO3 0.28 + pyo3-async-runtimes 0.28 bridging** is correct and well-
  covered. 307 unit tests + 6 heavyweight integration tests (asyncpg,
  SQLAlchemy async, FastAPI / Starlette) pass against real services.
- **Symbol-level drop-in compatibility** is real: 119/119 public asyncio
  symbols resolve through `ferro_io` via the `__getattr__` fallthrough.
- **`FastTaskGroup`** (Python-side optimization using `eager_task_factory` +
  inlined fast path for eagerly-completed tasks) shows 3.26× stdlib and
  1.60× uvloop on synthetic 10k-spawn benches. Useful only at that scale;
  invisible at typical application task counts.
- The code is a reasonable reference for PyO3 + Tokio + asyncio bridging if
  you are learning the stack.

### What was not real

- "947× faster" as a general claim about the runtime.
- "Multi-core async runtime for Python" — the runtime is multi-core; the
  Python code it runs is not, while the GIL remains.
- "Drop-in asyncio replacement that's faster" — drop-in replacement, yes.
  Faster for real workloads, no.

---

## Original documentation (archived)

The sections below reflect the pre-retirement README. They are preserved so
the original positioning and benchmark methodology remain reviewable.

### Two modes

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

### Original benchmarks (M-series Mac, 14 cores)

| Workload | stdlib asyncio | uvloop | ferro_io | vs stdlib |
|---|---:|---:|---:|---:|
| 50 × 50ms IO sleep | 51.62 ms | 51.57 ms | 51.44 ms | ~1× (sleep floor) |
| 200 × 20ms IO sleep | 22.94 ms | 22.00 ms | 21.23 ms | ~1× (sleep floor) |
| 14 CPU chains × 5M LCG iters | 6036 ms | 5578 ms | 6.37 ms | 947× |

Read the last row in light of the Findings section above: the "CPU chain"
workload is Rust arithmetic on one side vs Python arithmetic on the other,
not a runtime comparison.

### Coverage (as tested during active development)

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

### Build (reference only)

```bash
python -m venv .venv && source .venv/bin/activate
pip install maturin pytest pytest-asyncio
maturin develop --release
pytest tests/ -v
python benchmarks/bench.py
```

Built with PyO3 0.28 + pyo3-async-runtimes 0.28 + Tokio + maturin. ABI3
wheels covered Python 3.9+.

---

## License

MIT. See `LICENSE` (if present) or the repository root.
