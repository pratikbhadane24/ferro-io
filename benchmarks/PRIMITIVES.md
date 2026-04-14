# ferro_io primitive profiling

Per-primitive micro-workloads. Each column runs in its own
subprocess so `uvloop.install()` / `ferro_io.install()` can't
contaminate neighbours. Best of 5 trials. Speedup is relative
to stdlib asyncio.


| Workload | stdlib | uvloop | ferro_io | uvloop vs stdlib | ferro_io vs stdlib |
|---|---:|---:|---:|---:|---:|
| Queue SPSC (100k items) | 38.77 ms | 39.74 ms | 39.51 ms | 0.98× | 0.98× |
| Queue MPMC (4p/4c, 100k items) | 48.19 ms | 48.45 ms | 48.55 ms | 0.99× | 0.99× |
| Lock contention (8×10k ops) | 23.80 ms | 23.46 ms | 23.03 ms | 1.01× | 1.03× |
| Event ping-pong (20k iters) | 546.64 ms | 525.69 ms | 549.75 ms | 1.04× | 0.99× |
| create_task spawn (10k no-ops) | 20.93 ms | 12.95 ms | 10.96 ms | 1.62× | 1.91× |
| TaskGroup spawn (10k no-ops) | 18.91 ms | 9.27 ms | 5.80 ms | 2.04× | 3.26× |

## Recommendation

**Target primitive: `event_pingpong`** — Event ping-pong (20k iters). ferro_io is 1.05× the best-known column on this workload, which is the widest gap in the matrix. The follow-up spec should port the backing primitive to a Tokio-backed implementation and re-run this harness to confirm the gap closes.

Full ranking (widest headroom first):

1. `event_pingpong` — Event ping-pong (20k iters) — 1.05× best-known, 1.01× stdlib
2. `queue_spsc` — Queue SPSC (100k items) — 1.02× best-known, 1.02× stdlib
3. `queue_mpmc` — Queue MPMC (4p/4c, 100k items) — 1.01× best-known, 1.01× stdlib
4. `lock_contention` — Lock contention (8×10k ops) — 1.00× best-known, 0.97× stdlib
5. `task_spawn` — create_task spawn (10k no-ops) — 1.00× best-known, 0.52× stdlib
6. `taskgroup_spawn` — TaskGroup spawn (10k no-ops) — 1.00× best-known, 0.31× stdlib
