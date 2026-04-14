"""Verify popular third-party async libraries work under ferro_io.install().

These tests prove the process-wide drop-in actually works against libraries
with non-trivial asyncio internals — not just the symbol surface.

Each test runs in a subprocess so the install/import order is clean. We
can't undo `import httpx` mid-process once it's bound to a particular
asyncio module.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run_subprocess(script: str, timeout: float = 60) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO, timeout=timeout,
    )
    if proc.returncode != 0:
        pytest.fail(f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    last = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")][-1]
    return json.loads(last)


# --------------------------------------------------------------------------
# httpx
# --------------------------------------------------------------------------

httpx = pytest.importorskip("httpx")


def test_httpx_under_ferro_io_install():
    script = textwrap.dedent('''
        import json, asyncio as _real_asyncio
        import http.server, socketserver, threading

        # Minimal local HTTP server
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *a, **k):
                pass
        s = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        s.daemon_threads = True
        port = s.server_address[1]
        threading.Thread(target=s.serve_forever, daemon=True).start()

        import ferro_io
        ferro_io.install()
        import asyncio
        # After install, asyncio IS ferro_io.
        assert asyncio is ferro_io

        import httpx

        async def main():
            async with httpx.AsyncClient() as client:
                rs = await asyncio.gather(*[client.get(f"http://127.0.0.1:{port}/") for _ in range(20)])
            return [r.status_code for r in rs]

        codes = asyncio.run(main())
        print(json.dumps({"codes": codes}))
    ''')
    result = _run_subprocess(script)
    assert result["codes"] == [200] * 20


# --------------------------------------------------------------------------
# motor (MongoDB)
# --------------------------------------------------------------------------

motor = pytest.importorskip("motor.motor_asyncio")


def _mongo_reachable() -> bool:
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 27017))
        return True
    except Exception:
        return False
    finally:
        s.close()


@pytest.mark.skipif(not _mongo_reachable(), reason="mongod not running on 127.0.0.1:27017")
def test_motor_under_ferro_io_install():
    script = textwrap.dedent('''
        import json, os
        import ferro_io
        ferro_io.install()
        import asyncio
        import motor.motor_asyncio as motor_mod

        async def main():
            client = motor_mod.AsyncIOMotorClient(
                "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=3000
            )
            db = client["ferro_io_test"]
            coll = db[f"third_party_{os.getpid()}"]
            await coll.drop()
            await asyncio.gather(*[coll.insert_one({"i": i}) for i in range(20)])
            docs = await asyncio.gather(*[coll.find_one({"i": i}) for i in range(20)])
            count = sum(1 for d in docs if d is not None)
            await coll.drop()
            client.close()
            return count

        n = asyncio.run(main())
        print(json.dumps({"found": n}))
    ''')
    result = _run_subprocess(script)
    assert result["found"] == 20


# --------------------------------------------------------------------------
# aiohttp — server + client in-process
# --------------------------------------------------------------------------

aiohttp = pytest.importorskip("aiohttp")


def test_aiohttp_under_ferro_io_install():
    script = textwrap.dedent('''
        import json
        import ferro_io
        ferro_io.install()
        import asyncio
        from aiohttp import web, ClientSession

        async def handle(request):
            return web.Response(text="hello-ferro_io")

        async def main():
            app = web.Application()
            app.router.add_get("/", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]

            async with ClientSession() as sess:
                results = await asyncio.gather(*[
                    sess.get(f"http://127.0.0.1:{port}/") for _ in range(25)
                ])
                bodies = await asyncio.gather(*[r.text() for r in results])

            await runner.cleanup()
            return bodies

        bodies = asyncio.run(main())
        print(json.dumps({"bodies": bodies}))
    ''')
    result = _run_subprocess(script)
    assert result["bodies"] == ["hello-ferro_io"] * 25


# --------------------------------------------------------------------------
# aiosqlite — file-based async SQLite, no external server
# --------------------------------------------------------------------------

aiosqlite = pytest.importorskip("aiosqlite")


def test_aiosqlite_under_ferro_io_install(tmp_path):
    db_path = str(tmp_path / "test.db")
    script = textwrap.dedent(f'''
        import json
        import ferro_io
        ferro_io.install()
        import asyncio
        import aiosqlite

        async def main():
            async with aiosqlite.connect({db_path!r}) as db:
                await db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
                await asyncio.gather(*[
                    db.execute("INSERT INTO items (val) VALUES (?)", (f"row-{{i}}",))
                    for i in range(50)
                ])
                await db.commit()
                async with db.execute("SELECT COUNT(*) FROM items") as cur:
                    row = await cur.fetchone()
                    return row[0]

        n = asyncio.run(main())
        print(json.dumps({{"count": n}}))
    ''')
    result = _run_subprocess(script)
    assert result["count"] == 50


# --------------------------------------------------------------------------
# websockets — in-process WebSocket server + client
# --------------------------------------------------------------------------

websockets = pytest.importorskip("websockets")


def test_websockets_under_ferro_io_install():
    script = textwrap.dedent('''
        import json
        import ferro_io
        ferro_io.install()
        import asyncio
        import websockets

        async def echo_handler(ws):
            async for message in ws:
                await ws.send("echo:" + message)

        async def main():
            async with websockets.serve(echo_handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    msgs = ["hello", "world", "ferro_io"]
                    replies = []
                    for m in msgs:
                        await ws.send(m)
                        replies.append(await ws.recv())
                    return replies

        replies = asyncio.run(main())
        print(json.dumps({"replies": replies}))
    ''')
    result = _run_subprocess(script)
    assert result["replies"] == ["echo:hello", "echo:world", "echo:ferro_io"]


# --------------------------------------------------------------------------
# redis (redis-py asyncio client)
# --------------------------------------------------------------------------

def _redis_reachable() -> bool:
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 6379))
        return True
    except Exception:
        return False
    finally:
        s.close()


redis = pytest.importorskip("redis")


@pytest.mark.skipif(not _redis_reachable(), reason="redis not running on 127.0.0.1:6379")
def test_redis_under_ferro_io_install():
    script = textwrap.dedent('''
        import json, os
        import ferro_io
        ferro_io.install()
        import asyncio
        import redis.asyncio as aioredis

        async def main():
            r = aioredis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            prefix = f"ferro_io_test_{os.getpid()}"
            try:
                await asyncio.gather(*[
                    r.set(f"{prefix}:{i}", f"val-{i}") for i in range(50)
                ])
                values = await asyncio.gather(*[
                    r.get(f"{prefix}:{i}") for i in range(50)
                ])
                ok = all(values[i] == f"val-{i}" for i in range(50))
                await r.delete(*[f"{prefix}:{i}" for i in range(50)])
            finally:
                await r.aclose()
            return ok

        ok = asyncio.run(main())
        print(json.dumps({"ok": ok}))
    ''')
    result = _run_subprocess(script)
    assert result["ok"] is True
