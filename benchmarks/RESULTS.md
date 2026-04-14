# ferro_io benchmark matrix

Best of N trials per row. Speedup is relative to stdlib asyncio.


## Workload A — 50 × 50ms IO sleep

| Implementation | Best | vs stdlib |
|---|---|---|
| stdlib asyncio | 51.62 ms | 1.0× |
| ThreadPoolExecutor | 58.70 ms | 0.9× |
| uvloop | 51.53 ms | 1.0× |
| ferro_io.AsyncRuntime (direct) | 51.95 ms | 1.0× |
| ferro_io drop-in (import swap) | 54.66 ms | 0.9× |

## Workload B — 200 × 20ms IO sleep

| Implementation | Best | vs stdlib |
|---|---|---|
| stdlib asyncio | 22.71 ms | 1.0× |
| ThreadPoolExecutor | 95.49 ms | 0.2× |
| uvloop | 22.37 ms | 1.0× |
| ferro_io.AsyncRuntime (direct) | 22.19 ms | 1.0× |
| ferro_io drop-in (import swap) | 28.90 ms | 0.8× |

## Workload C — CPU-bound LCG chains (headline)

| Implementation | Best | vs stdlib |
|---|---|---|
| asyncio (no parallelism) | 5979.27 ms | 1.0× |
| ThreadPoolExecutor (GIL!) | 5524.56 ms | 1.1× |
| uvloop | 5622.67 ms | 1.1× |
| ferro_io.map_blocking | 6.31 ms | 948.0× |
