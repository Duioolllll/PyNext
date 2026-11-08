import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = _load("pynext_baseline", ROOT / "benchmarks" / "baseline.py")
collect_env = _load("pynext_collect_env", ROOT / "benchmarks" / "collect_env.py")


def test_baseline_covers_the_seven_roadmap_scenarios():
    names = [scenario["name"] for scenario in baseline.SCENARIOS]
    assert names == ["static", "ssg", "isr-hit", "ssr", "json-api", "db-query", "streaming"]
    paths = {scenario["name"]: scenario["path"] for scenario in baseline.SCENARIOS}
    assert paths["static"] == "/styles/site.css"
    assert paths["ssg"] == "/about"
    assert paths["isr-hit"] == "/news/hello"
    assert paths["ssr"] == "/"
    assert paths["json-api"] == "/api/users"
    assert paths["db-query"] == "/api/db-users?id=42"
    assert paths["streaming"] == "/api/stream"


def test_baseline_config_has_fixed_repeated_conditions():
    config = baseline._load_config(ROOT / "benchmarks" / "baseline-config.json")
    assert config["requests"] == 2000
    assert config["concurrency"] == 10
    assert config["repetitions"] == 3
    assert config["production"] is True
    assert config["modes"] == ["axum", "uvicorn"]
    assert config["scenarios"] == [s["name"] for s in baseline.SCENARIOS]


def test_baseline_config_rejects_unknown_modes_and_scenarios(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"requests": 10, "concurrency": 2, "warmup": 1,
                               "repetitions": 1, "modes": ["warp"],
                               "scenarios": ["json-api"], "production": False}))
    with pytest.raises(ValueError, match="modes unknown"):
        baseline._load_config(bad)
    bad.write_text(json.dumps({"requests": 10, "concurrency": 2, "warmup": 1,
                               "repetitions": 1, "modes": ["axum"],
                               "scenarios": ["nope"], "production": False}))
    with pytest.raises(ValueError, match="scenarios unknown"):
        baseline._load_config(bad)


def test_aggregate_reports_p99_9_error_rate_and_server_resources():
    repetitions = [
        {"successful_latency_ms": {"p50": 10.0, "p95": 20.0, "p99": 30.0, "p99.9": 40.0},
         "successful_requests_per_second": 100.0, "error_rate": 0.0, "errors": 0,
         "server_cpu_seconds": 1.0, "process_peak_rss_bytes": 50_000_000,
         "process_cpu_seconds": 0.5,
         "response_bytes": {"mean_per_response": 25.0}},
        {"successful_latency_ms": {"p50": 12.0, "p95": 22.0, "p99": 32.0, "p99.9": 44.0},
         "successful_requests_per_second": 90.0, "error_rate": 0.01, "errors": 1,
         "server_cpu_seconds": 1.2, "process_peak_rss_bytes": 52_000_000,
         "process_cpu_seconds": 0.6,
         "response_bytes": {"mean_per_response": 25.0}},
    ]
    result = baseline.aggregate(repetitions)
    assert result["latency_ms"] == {"p50": 11.0, "p95": 21.0, "p99": 31.0, "p99.9": 42.0}
    assert result["successful_requests_per_second"] == 95.0
    assert result["error_rate_mean"] == 0.005
    assert result["error_rate_max"] == 0.01
    assert result["errors_total"] == 1
    assert result["server_cpu_seconds_mean"] == 1.1
    assert result["server_peak_rss_bytes_mean"] == 51_000_000
    assert result["loadgen_cpu_seconds_mean"] == 0.55


def test_collect_env_records_both_machines_facts():
    env = collect_env.collect(ROOT)
    for key in ("hostname", "cpu_count_logical", "os", "python", "pynext",
                "uvicorn", "httpx", "git"):
        assert key in env
    assert env["git"]["sha"]


def test_db_fixture_returns_a_seeded_row():
    from pynext import create_app

    fixture = ROOT / "benchmarks" / "fixtures" / "db-app"
    # resources.py at the fixture root is discovered by create_app; passing
    # it explicitly as well would collide on the "db" name.
    app = create_app(fixture)

    async def check():
        await app.startup()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test", trust_env=False) as client:
                response = await client.get("/api/db-users?id=42")
                assert response.status_code == 200
                assert response.json() == {"id": 42, "name": "user-0042"}
                assert (await client.get("/api/db-users?id=1001")).status_code == 404
                assert (await client.get("/api/db-users?id=nope")).status_code == 400
        finally:
            await app.shutdown()

    asyncio.run(check())


def test_baseline_smoke_run_writes_p99_9_and_environment(tmp_path):
    config = {"requests": 4, "concurrency": 2, "warmup": 1, "repetitions": 1,
              "modes": ["axum"], "scenarios": ["json-api", "db-query"],
              "production": False, "_config_path": "test"}
    output = tmp_path / "baseline.json"
    result = asyncio.run(baseline.run_baseline(config, output=output))
    assert output.is_file()
    assert result["schema_version"] == 1
    assert result["conditions"]["loadgen_location"] == "same-machine"
    assert "same-machine" in " ".join(result["limitations"])
    assert result["environment"]["load_generator"]["pynext"] is not None
    by_name = {s["name"]: s for s in result["scenarios"]}
    for name in ("json-api", "db-query"):
        aggregate = by_name[name]["results"]["axum"]["aggregate"]
        assert set(aggregate["latency_ms"]) == {"p50", "p95", "p99", "p99.9"}
        assert aggregate["error_rate_max"] == 0
        assert aggregate["server_peak_rss_bytes_mean"] > 0
        assert aggregate["server_cpu_seconds_mean"] is not None


def test_baseline_external_unreachable_server_records_errors_not_crash(tmp_path):
    config = {"requests": 2, "concurrency": 1, "warmup": 0, "repetitions": 1,
              "modes": ["axum"], "scenarios": ["json-api"],
              "production": False, "_config_path": "test"}
    output = tmp_path / "baseline.json"
    result = asyncio.run(baseline.run_baseline(
        config, output=output, base_url="http://127.0.0.1:1"))
    aggregate = result["scenarios"][0]["results"]["axum"]["aggregate"]
    assert aggregate["error_rate_max"] == 1
    assert aggregate["latency_ms"]["p50"] is None
    assert result["conditions"]["loadgen_location"] == "separate-machine"
    assert result["environment"]["server"]["environment"].startswith("unknown")
