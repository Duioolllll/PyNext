import asyncio
import importlib.util
import os
import select
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("pynext_benchmark", Path(__file__).resolve().parents[1] / "benchmarks/bench.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
measure, run = benchmark.measure, benchmark.run


def test_benchmark_distinguishes_success_from_http_and_transport_errors():
    async def check():
        outcomes = iter([503, 200, 201, 404, 503, 504, "timeout"])

        def respond(request):
            outcome = next(outcomes)
            if outcome == "timeout":
                raise httpx.ReadTimeout("simulated", request=request)
            return httpx.Response(outcome, content=b"ok")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://test") as client:
            result = await measure(client, "/", 6, 2, warmup=1)
        assert result["warmup_errors"] == 1
        assert result["successful_requests"] == 2
        assert result["errors"] == 4 and result["error_rate"] == 0.666667
        assert result["http_status_counts"] == {"200": 1, "201": 1, "404": 1, "503": 1, "504": 1}
        assert result["http_status_class_counts"] == {"2xx": 2, "4xx": 1, "5xx": 2}
        assert result["transport_error_counts"] == {"ReadTimeout": 1}
        assert result["attempted_requests_per_second"] > result["successful_requests_per_second"] > 0
        assert set(result["latency_ms"]) == {"p50", "p95", "p99", "p99.9"}
        assert set(result["successful_latency_ms"]) == {"p50", "p95", "p99", "p99.9"}
        assert result["response_bytes"] == {"total": 10, "mean_per_response": 2.0}
        assert result["process_cpu_scope"] == "load_generator_and_server"
        assert result["process_peak_rss_bytes"] is None or result["process_peak_rss_bytes"] > 0
        assert result["server_cpu_seconds"] is None

    asyncio.run(check())


@pytest.mark.parametrize("mode, transport", [
    ("axum", "loopback_http"),
    ("uvicorn", "loopback_http"),
    ("asgi", "in_process_asgi"),
    ("starlette", "loopback_http"),
])
def test_benchmark_mode_transport_labels_and_success(mode, transport):
    result = asyncio.run(run(mode, 4, 2, "/api/users", False, warmup=1))
    assert result["mode"] == mode
    assert result["transport"] == transport
    assert result["successful_requests"] == 4
    assert result["errors"] == 0


def test_starlette_reference_returns_the_same_api_payload():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pynext_starlette_app",
        Path(__file__).resolve().parents[1] / "benchmarks" / "starlette_app.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def check():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/users")).json() == {"users": ["Alice", "Bob"]}
            response = await client.post("/api/users", json={"name": "N"})
            assert response.status_code == 201 and response.json() == {"name": "N"}
            assert (await client.get("/")).status_code == 200
            assert "Hello PyNext!" in (await client.get("/")).text
            assert (await client.get("/blog/hello")).status_code == 200

    asyncio.run(check())


def test_benchmark_compare_runs_all_loopback_groups():
    result = asyncio.run(benchmark.run_compare(4, 2, "/api/users", False, warmup=1))
    assert [group["mode"] for group in result["groups"]] == ["axum", "uvicorn", "starlette"]
    assert all(group["transport"] == "loopback_http" for group in result["groups"])
    assert all(group["successful_requests"] == 4 and group["errors"] == 0 for group in result["groups"])
    assert all(set(group["latency_ms"]) == {"p50", "p95", "p99", "p99.9"} for group in result["groups"])
    assert len({group["server_pid"] for group in result["groups"]}) == 3
    assert all(group["process_peak_rss_bytes"] > 0 and group["process_peak_rss_scope"] == "server_sampled_10ms"
               for group in result["groups"])
    assert result["conditions"]["path"] == "/api/users"


def test_benchmark_reports_zero_success_without_fabricating_success_latency():
    async def check():
        transport = httpx.MockTransport(lambda request: httpx.Response(503, content=b"busy"))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await measure(client, "/", 3, 2, warmup=0)
        assert result["successful_requests"] == 0 and result["errors"] == 3
        assert result["successful_requests_per_second"] == 0
        assert result["successful_latency_ms"] is None
        assert result["http_status_counts"] == {"503": 3}

    asyncio.run(check())


def test_benchmark_does_not_attribute_reused_pid_cpu(monkeypatch):
    values = iter([(2.0, 10), (3.0, 11)])
    monkeypatch.setattr(benchmark, "_linux_process_cpu", lambda pid: next(values))

    async def check():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await measure(client, "/", 1, 1, warmup=0, server_pid=123, external=True)
        assert result["server_cpu_seconds"] is None
        assert result["server_cpu_percent_one_core"] is None
        assert result["server_cpu_error"] == "server process exited or PID was reused"

    asyncio.run(check())


@pytest.mark.skipif(sys.platform != "linux" or not os.path.exists("/proc/self/stat"),
                    reason="separate process CPU sampling requires Linux procfs")
def test_benchmark_external_server_cpu_is_separate_from_load_generator():
    code = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
"""
    server = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([server.stdout], [], [], 10)
        assert ready, "test server did not start"
        port = int(server.stdout.readline())
        result = asyncio.run(run("axum", 4, 2, "/", False, warmup=0,
                                 base_url=f"http://127.0.0.1:{port}", server_pid=server.pid))
        assert result["mode"] == "external"
        assert result["transport"] == "external_http"
        assert result["successful_requests"] == 4 and result["errors"] == 0
        assert result["process_cpu_scope"] == "load_generator_only"
        assert result["server_pid"] == server.pid
        assert result["server_cpu_seconds"] >= 0
        assert result["server_cpu_percent_one_core"] >= 0
        assert result["server_cpu_error"] is None
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_benchmark_production_requires_an_explicit_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bench.py", "--production"])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert "pynext build --dir examples/basic" in capsys.readouterr().err
