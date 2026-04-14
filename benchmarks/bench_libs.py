"""Benchmark third-party async libraries under stdlib asyncio vs
`ferro_io.install()` (process-wide drop-in mode).

Each variant runs in its own subprocess so we get a clean import graph —
once a library has imported asyncio, you can't unwind that mid-process.

Workloads
---------
1. **httpx**: 100 parallel GETs against a local stdlib HTTP server.
2. **aiohttp**: 100 parallel GETs against a local aiohttp server (server +
   client both async).
3. **aiosqlite**: 200 parallel INSERTs against an in-memory SQLite DB.
4. **websockets**: 200 echo round-trips against a local ws server.
5. **redis** (redis-py asyncio): 500 parallel SET + 500 parallel GET against
   localhost:6379.
6. **motor (MongoDB)**: 200 parallel inserts + 200 parallel finds against
   the local mongod at 127.0.0.1:27017.

Run:
    python benchmarks/bench_libs.py
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRIALS = 5

# ---------------------------------------------------------------------------
# Worker scripts — these are executed as `python -c "..."` in subprocesses.
# Each one prints a single JSON line: {"label": ..., "samples": [...]}
# ---------------------------------------------------------------------------

HTTPX_WORKER = r'''
import json, sys, time
import http.server, socketserver, threading

# Tiny local HTTP server in a background thread.
class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **k):
        pass

server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Quiet)
server.daemon_threads = True
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

# Import httpx AFTER potential install() so it picks up the patched asyncio.
import asyncio
import httpx

URL = f"http://127.0.0.1:{PORT}/"

async def trial(client):
    await asyncio.gather(*[client.get(URL) for _ in range(N)])

async def main():
    async with httpx.AsyncClient() as client:
        # warmup
        await trial(client)
        samples = []
        for _ in range(TRIALS):
            t0 = time.perf_counter()
            await trial(client)
            samples.append(time.perf_counter() - t0)
        return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''

MOTOR_WORKER = r'''
import json, sys, time, os

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

import asyncio
import motor.motor_asyncio as motor_mod

async def main():
    client = motor_mod.AsyncIOMotorClient(
        "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=3000
    )
    db = client["ferro_io_bench"]
    coll = db[f"bench_{MODE}_{os.getpid()}"]
    await coll.drop()

    async def trial():
        docs = [{"i": i, "payload": "x" * 64} for i in range(N)]
        await asyncio.gather(*[coll.insert_one(d) for d in docs])
        await asyncio.gather(*[coll.find_one({"i": i}) for i in range(N)])
        await coll.delete_many({})

    # warmup
    await trial()

    samples = []
    for _ in range(TRIALS):
        t0 = time.perf_counter()
        await trial()
        samples.append(time.perf_counter() - t0)

    await coll.drop()
    client.close()
    return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''


def run_worker(script: str, mode: str, n: int) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, mode, str(TRIALS), str(n)],
        capture_output=True, text=True, cwd=REPO, timeout=300,
    )
    if proc.returncode != 0:
        print(f"[{mode}] subprocess failed:\n{proc.stderr}", file=sys.stderr)
        return None
    # Last line of stdout is the JSON result.
    last = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")][-1]
    return json.loads(last)


def fmt_ms(s: float) -> str:
    return f"{s * 1000:8.2f} ms"


def report(name: str, n: int, results: list[dict]) -> None:
    print(f"\n{name} ({n} parallel ops × {TRIALS} trials)")
    print(f"  {'mode':<22s}  {'best':>11s}  {'p50':>11s}  {'mean':>11s}")
    baseline = None
    for r in results:
        if r is None:
            continue
        s = sorted(r["samples"])
        best, p50, mean = s[0], s[len(s) // 2], statistics.mean(s)
        if baseline is None:
            baseline = best
            ratio = "1.00×"
        else:
            ratio = f"{baseline / best:.2f}×"
        print(f"  {r['label']:<22s}  {fmt_ms(best)}  {fmt_ms(p50)}  {fmt_ms(mean)}  ({ratio})")


AIOHTTP_WORKER = r'''
import json, sys, time

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

import asyncio
from aiohttp import web, ClientSession

async def handle(request):
    return web.Response(text="ok")

async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    async with ClientSession() as sess:
        async def trial():
            results = await asyncio.gather(*[sess.get(url) for _ in range(N)])
            await asyncio.gather(*[r.text() for r in results])
        await trial()  # warmup
        samples = []
        for _ in range(TRIALS):
            t0 = time.perf_counter()
            await trial()
            samples.append(time.perf_counter() - t0)

    await runner.cleanup()
    return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''

AIOSQLITE_WORKER = r'''
import json, sys, time

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

import asyncio
import aiosqlite

async def main():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")

        async def trial():
            await db.execute("DELETE FROM items")
            await asyncio.gather(*[
                db.execute("INSERT INTO items (val) VALUES (?)", (f"row-{i}",))
                for i in range(N)
            ])
            await db.commit()

        await trial()  # warmup
        samples = []
        for _ in range(TRIALS):
            t0 = time.perf_counter()
            await trial()
            samples.append(time.perf_counter() - t0)
        return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''

WEBSOCKETS_WORKER = r'''
import json, sys, time

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

import asyncio
import websockets

async def echo(ws):
    async for m in ws:
        await ws.send(m)

async def main():
    async with websockets.serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            async def trial():
                for i in range(N):
                    await ws.send(f"msg-{i}")
                    await ws.recv()

            await trial()  # warmup
            samples = []
            for _ in range(TRIALS):
                t0 = time.perf_counter()
                await trial()
                samples.append(time.perf_counter() - t0)
            return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''

REDIS_WORKER = r'''
import json, sys, time, os

MODE = sys.argv[1]
TRIALS = int(sys.argv[2])
N = int(sys.argv[3])

if MODE == "ferro_io_install":
    import ferro_io
    ferro_io.install()

import asyncio
import redis.asyncio as aioredis

async def main():
    r = aioredis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    prefix = f"bench_{MODE}_{os.getpid()}"
    keys = [f"{prefix}:{i}" for i in range(N)]
    try:
        async def trial():
            await asyncio.gather(*[r.set(k, "x") for k in keys])
            await asyncio.gather(*[r.get(k) for k in keys])

        await trial()  # warmup
        samples = []
        for _ in range(TRIALS):
            t0 = time.perf_counter()
            await trial()
            samples.append(time.perf_counter() - t0)
    finally:
        if keys:
            await r.delete(*keys)
        await r.aclose()
    return samples

samples = asyncio.run(main())
print(json.dumps({"label": MODE, "samples": samples}))
'''


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main() -> None:
    print(f"ferro_io third-party library benchmark (cpu_count={os.cpu_count()}, trials={TRIALS})")

    suites = [
        ("httpx",      "100 parallel GETs (stdlib http server)",      HTTPX_WORKER,      100, True),
        ("aiohttp",    "100 parallel GETs (aiohttp server+client)",   AIOHTTP_WORKER,    100, True),
        ("aiosqlite",  "200 parallel INSERTs (in-memory SQLite)",     AIOSQLITE_WORKER,  200, True),
        ("websockets", "200 echo round-trips (local ws server)",      WEBSOCKETS_WORKER, 200, True),
        ("redis",      "500 SET + 500 GET (localhost:6379)",          REDIS_WORKER,      500, _port_open(6379)),
        ("motor",      "200 inserts + 200 finds (localhost:27017)",   MOTOR_WORKER,      200, _port_open(27017)),
    ]

    for name, desc, worker, n, available in suites:
        print(f"\n=== {name} — {desc} ===")
        if not available:
            print(f"  skipped: service unreachable")
            continue
        results = []
        for mode in ("stdlib_asyncio", "ferro_io_install"):
            print(f"  running {mode}...", flush=True)
            results.append(run_worker(worker, mode, n))
        report(name, n, results)


if __name__ == "__main__":
    main()
