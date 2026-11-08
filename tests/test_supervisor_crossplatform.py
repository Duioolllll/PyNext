"""Exercise spawned workers sharing a listener on each CI operating system."""
import multiprocessing
import socket

import httpx

from pynext.supervisor import WorkerConfig, _Worker


def test_spawned_workers_share_listener_and_restart(tmp_path):
    directory = tmp_path / "app" / "who"
    directory.mkdir(parents=True)
    (directory / "route.py").write_text(
        'import os\nfrom pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"pid": os.getpid()})\n', encoding="utf-8")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    host, port = listener.getsockname()
    config = WorkerConfig(root=str(tmp_path), host=host, port=port, production=False,
                          max_concurrent=16, acquire_timeout_ms=1000, body_capacity=4, body_timeout_ms=10000)
    context = multiprocessing.get_context("spawn")
    workers = [_Worker(index, config, context, listener) for index in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            assert worker.ready.wait(30), f"worker {worker.index} did not start"
        pids = {worker.pid for worker in workers}
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=5, trust_env=False) as client:
            assert client.get("/who").json()["pid"] in pids
        victim = workers[0]
        old = victim.pid
        victim.kill()
        victim.join(timeout=10)
        victim.start()
        assert victim.ready.wait(30)
        assert victim.pid != old
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=5, trust_env=False) as client:
            assert client.get("/who").json()["pid"] in {worker.pid for worker in workers}
    finally:
        for worker in workers:
            worker.terminate()
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            if worker.process is not None:
                worker.process.close()
        listener.close()
