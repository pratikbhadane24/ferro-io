"""Phase 8c: real-world programs run through ferro_io.run()."""
import os
import sys
import time

import pytest
import ferro_io


# 1. TaskGroup producer/consumer pipeline (3.11+)
@pytest.mark.skipif(not hasattr(ferro_io, "TaskGroup"), reason="TaskGroup is 3.11+")
def test_taskgroup_producer_consumer_pipeline():
    async def main():
        q = ferro_io.Queue()
        produced = []
        consumed = []

        async def producer(idx):
            for i in range(3):
                item = idx * 10 + i
                await q.put(item)
                produced.append(item)
            await q.put(None)

        async def consumer():
            done_signals = 0
            while done_signals < 3:
                item = await q.get()
                if item is None:
                    done_signals += 1
                    continue
                consumed.append(item)

        async with ferro_io.TaskGroup() as tg:
            tg.create_task(consumer())
            for idx in range(3):
                tg.create_task(producer(idx))

        return sorted(produced), sorted(consumed)

    produced, consumed = ferro_io.run(main())
    assert len(produced) == 9
    assert produced == consumed


# 2. Subprocess pipeline
@pytest.mark.skipif(sys.platform == "win32", reason="shell echo varies on Windows")
def test_subprocess_echo():
    async def main():
        proc = await ferro_io.create_subprocess_shell(
            "echo hello-ferro_io",
            stdout=ferro_io.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    assert ferro_io.run(main()) == "hello-ferro_io"


# 3. TCP loopback streams
def test_tcp_loopback_echo():
    payload = b"x" * 1024

    async def main():
        received = []

        async def handle(reader, writer):
            data = await reader.read(len(payload))
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await ferro_io.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def client():
            r, w = await ferro_io.open_connection("127.0.0.1", port)
            w.write(payload)
            await w.drain()
            data = await r.read(len(payload))
            received.append(data)
            w.close()
            await w.wait_closed()

        await client()
        server.close()
        await server.wait_closed()
        return received[0]

    assert ferro_io.run(main()) == payload


# 4. to_thread for blocking work
@pytest.mark.skipif(not hasattr(ferro_io, "to_thread"), reason="to_thread is 3.9+")
def test_to_thread_parallelism():
    def blocking_unit():
        time.sleep(0.05)
        return os.getpid()

    async def main():
        start = time.perf_counter()
        results = await ferro_io.gather(*[ferro_io.to_thread(blocking_unit) for _ in range(10)])
        return time.perf_counter() - start, results

    elapsed, results = ferro_io.run(main())
    assert len(results) == 10
    # Serial would be 10×50ms = 500ms; 0.45s proves parallelism with CI headroom.
    assert elapsed < 0.45, f"to_thread parallelism failed: {elapsed:.3f}s"
