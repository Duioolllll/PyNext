"""Reproducible load baseline (roadmap C1).

Runs the fixed scenario set against the fixed example app, repeating every
measurement, and writes one baseline JSON with p50/p95/p99/p99.9 latency,
error rate, CPU and RSS -- plus the full environment on both sides of the
connection so a later run can be compared honestly.

Two topologies:

* ``local`` (default): the script starts one server process per mode on this
  machine and measures it over loopback HTTP. The load generator and the
  server share the machine, so this is a smoke/regression baseline, not a
  production throughput claim. The output records
  ``loadgen_location: "same-machine"``.
* ``external`` (``--base-url``): the server already runs somewhere else --
  ideally a separate machine. Run ``benchmarks/collect_env.py`` on that
  machine first and pass the file with ``--server-info`` so the baseline
  records the server hardware too. The DB-query scenario needs its own
  server; pass ``--db-base-url`` or it is recorded as skipped.

Scenarios (all paths are fixed by this file; see ``SCENARIOS``):

* ``static``    -- ``/styles/site.css`` (public asset)
* ``ssg``       -- ``/about`` (built once, served as static HTML)
* ``isr-hit``   -- ``/news/hello`` (warmup primes the ISR entry; measured
  requests are cache hits inside the revalidate window)
* ``ssr``       -- ``/`` (server-rendered page with an island)
* ``json-api``  -- ``GET /api/users``
* ``db-query``  -- ``GET /api/db-users?id=42`` against the benchmark-only
  SQLite fixture app (``benchmarks/fixtures/db-app``); a point lookup by
  primary key over 1,000 seeded rows. This is benchmark infrastructure,
  not a product DB integration (roadmap E4).
* ``streaming`` -- ``GET /api/stream`` (chunked NDJSON response)

Defaults live in ``benchmarks/baseline-config.json``; ``--smoke`` overrides
them with a tiny run for CI/script verification. ``--production`` (the
default) requires a build manifest for ``examples/basic``; the script runs
``pynext build`` when the manifest is missing.

Usage (local smoke):
    uv run python benchmarks/baseline.py --smoke

Usage (full local baseline):
    uv run python benchmarks/baseline.py --output benchmarks/baseline-2026-11-08.json

Usage (separate load-generator machine; server started on another host):
    # server machine:
    python benchmarks/collect_env.py --output server-env.json
    uv run pynext start --dir examples/basic --host 0.0.0.0 --port 3000
    # load-generator machine:
    python benchmarks/baseline.py --base-url http://<server>:3000 \\
        --server-info server-env.json --output baseline-remote.json
"""
import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from bench import measure  # noqa: E402
from collect_env import collect as collect_environment  # noqa: E402

BASIC_DIR = ROOT / "examples" / "basic"
DB_FIXTURE_DIR = HERE / "fixtures" / "db-app"
DEFAULT_CONFIG = HERE / "baseline-config.json"
MANIFEST = BASIC_DIR / ".pynext" / "manifest.json"

SCENARIOS = [
    {"name": "static", "path": "/styles/site.css", "app": "basic",
     "description": "Public static asset"},
    {"name": "ssg", "path": "/about", "app": "basic",
     "description": "SSG page, built once and served as static HTML"},
    {"name": "isr-hit", "path": "/news/hello", "app": "basic",
     "description": "ISR page; warmup primes the entry, measured requests are hits"},
    {"name": "ssr", "path": "/", "app": "basic",
     "description": "Server-rendered page with an island"},
    {"name": "json-api", "path": "/api/users", "app": "basic",
     "description": "JSON API route"},
    {"name": "db-query", "path": "/api/db-users?id=42", "app": "db",
     "description": "SQLite point lookup by primary key over 1,000 seeded rows"},
    {"name": "streaming", "path": "/api/stream", "app": "basic",
     "description": "Chunked NDJSON streaming response"},
]

# The Starlette reference app only implements this subset of the example app.
STARLETTE_SCENARIOS = {"ssr": "/", "ssg": "/about", "json-api": "/api/users"}

MODES = ("axum", "uvicorn", "starlette")


def _median(values):
    return round(statistics.median(values), 3) if values else None


def _mean(values):
    return round(statistics.mean(values), 3) if values else None


def aggregate(repetitions):
    """Summarise repeated measurements of one scenario/mode.

    Latencies are medians across repetitions (robust to one slow run);
    throughput and error rate are means, with the worst error rate kept
    alongside so a flaky repetition cannot hide behind an average.
    """
    latency_keys = ("p50", "p95", "p99", "p99.9")
    latency = {}
    for key in latency_keys:
        values = [rep["successful_latency_ms"][key] for rep in repetitions
                  if rep.get("successful_latency_ms")]
        latency[key] = _median(values)
    server_cpu = [rep["server_cpu_seconds"] for rep in repetitions
                  if rep.get("server_cpu_seconds") is not None]
    server_rss = [rep["process_peak_rss_bytes"] for rep in repetitions
                  if rep.get("process_peak_rss_bytes")]
    response_bytes = [rep["response_bytes"]["mean_per_response"] for rep in repetitions
                      if rep["response_bytes"].get("mean_per_response") is not None]
    return {
        "repetitions": len(repetitions),
        "latency_ms": latency,
        "successful_requests_per_second": _mean(
            [rep["successful_requests_per_second"] for rep in repetitions]),
        "error_rate_mean": _mean([rep["error_rate"] for rep in repetitions]),
        "error_rate_max": max((rep["error_rate"] for rep in repetitions), default=None),
        "errors_total": sum(rep["errors"] for rep in repetitions),
        "server_cpu_seconds_mean": _mean(server_cpu),
        "server_peak_rss_bytes_mean": int(_mean(server_rss)) if server_rss else None,
        "loadgen_cpu_seconds_mean": _mean([rep["process_cpu_seconds"] for rep in repetitions]),
        "response_mean_bytes": _mean(response_bytes),
    }


def ensure_build():
    """Build examples/basic when --production is requested without a manifest."""
    if not (DB_FIXTURE_DIR / ".pynext/manifest.json").is_file():
        subprocess.run([sys.executable, "-m", "pynext.cli", "build", "--dir", str(DB_FIXTURE_DIR)],
                       check=True, capture_output=True, timeout=600)
    if MANIFEST.is_file():
        return None
    pynext = shutil.which("pynext")
    if pynext is None:
        raise RuntimeError(
            "production baseline needs examples/basic built first: run "
            "`pynext build --dir examples/basic` (no `pynext` executable on PATH)")
    result = subprocess.run([pynext, "build", "--dir", str(BASIC_DIR)],
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"`pynext build --dir examples/basic` failed:\n{result.stderr}")
    return "built examples/basic for production baseline"


class _ServerProcess:
    """One benchmark server subprocess with 10 ms RSS sampling (Linux/any OS)."""

    def __init__(self, command, ready_prefix="PYNEXT_BENCH_READY "):
        self.command = command
        self.ready_prefix = ready_prefix
        self.process = None
        self.address = None
        self._rss_samples = []
        self._sampler = None
        self._psutil_process = None
        self._stderr_tail = bytearray()
        self._drainers = []

    async def __aenter__(self):
        import psutil

        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        async def drain(stream, tail=None):
            while chunk := await stream.read(8192):
                if tail is not None:
                    tail.extend(chunk)
                    del tail[:-8192]

        # Access logs must not fill a PIPE and stall an otherwise healthy
        # server in a repeated run. Retain only a bounded diagnostic tail.
        self._drainers.append(asyncio.create_task(drain(self.process.stderr, self._stderr_tail)))
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), 30)
        except TimeoutError as exc:
            await self._stop_process()
            raise RuntimeError(f"benchmark server did not start: {' '.join(self.command)}") from exc
        if not line.startswith(self.ready_prefix.encode()):
            await self._stop_process()
            detail = self._stderr_tail.decode("utf-8", "replace").strip()[-2000:]
            raise RuntimeError(
                f"benchmark server did not start: {' '.join(self.command)}\n{detail}")
        self.address = line.decode("ascii").strip().split(" ", 1)[1]
        self._drainers.append(asyncio.create_task(drain(self.process.stdout)))
        server_process = psutil.Process(self.process.pid)
        self._psutil_process = server_process
        stop = asyncio.Event()
        self._stop_sampler = stop

        async def sample_rss():
            while not stop.is_set():
                try:
                    self._rss_samples.append(server_process.memory_info().rss)
                except psutil.Error:
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.01)
                except TimeoutError:
                    pass

        self._sampler = asyncio.create_task(sample_rss())
        return self

    async def _stop_process(self):
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._drainers:
            await asyncio.gather(*self._drainers)

    @property
    def pid(self):
        return self.process.pid if self.process else None

    @property
    def peak_rss(self):
        return max(self._rss_samples) if self._rss_samples else None

    async def __aexit__(self, *exc):
        if self._sampler is not None:
            # One final sample: a very short measurement could otherwise
            # finish before the 10 ms sampler ever fires.
            try:
                self._rss_samples.append(self._psutil_process.memory_info().rss)
            except Exception:
                pass
            self._stop_sampler.set()
            await self._sampler
        await self._stop_process()


def _server_command(mode, app, production):
    """Build the server command for one mode/app combination."""
    if mode == "starlette":
        # The Starlette reference app only implements the basic example app;
        # serve_project.py cannot serve it, so use the existing helper.
        if app != "basic":
            raise ValueError("starlette mode cannot serve the DB fixture app")
        command = [sys.executable, str(HERE / "server_process.py"), "--mode", "starlette"]
        if production:
            command.append("--production")
        return command
    command = [sys.executable, str(HERE / "serve_project.py"), "--mode", mode]
    if app == "db":
        command += ["--dir", str(DB_FIXTURE_DIR)]
    else:
        command += ["--dir", str(BASIC_DIR)]
    if production:
        command.append("--production")
    return command


async def _measure_local(scenario, mode, config):
    """Measure one scenario against a freshly started local server process."""
    command = _server_command(mode, scenario["app"], config["production"])
    async with _ServerProcess(command) as server:
        async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                     timeout=30, trust_env=False) as client:
            result = await measure(client, scenario["path"], config["requests"],
                                   config["concurrency"], warmup=config["warmup"],
                                   server_pid=server.pid,
                                   external=True)
    result["process_peak_rss_bytes"] = server.peak_rss
    result["process_peak_rss_scope"] = "server_sampled_10ms"
    result["server_pid"] = server.pid
    return result


async def _measure_external(client, scenario, config):
    return await measure(client, scenario["path"], config["requests"],
                         config["concurrency"], warmup=config["warmup"], external=True)


def _load_config(path):
    with open(path) as handle:
        config = json.load(handle)
    for key in ("requests", "concurrency", "repetitions"):
        if not isinstance(config.get(key), int) or isinstance(config[key], bool) \
                or config[key] < 1:
            raise ValueError(f"baseline config {key} must be a positive integer")
    if not isinstance(config.get("warmup"), int) or isinstance(config["warmup"], bool) \
            or config["warmup"] < 0:
        raise ValueError("baseline config warmup must be a non-negative integer")
    unknown_modes = set(config["modes"]) - set(MODES)
    if unknown_modes:
        raise ValueError(f"baseline config modes unknown: {sorted(unknown_modes)}")
    known = {scenario["name"] for scenario in SCENARIOS}
    unknown_scenarios = set(config["scenarios"]) - known
    if unknown_scenarios:
        raise ValueError(f"baseline config scenarios unknown: {sorted(unknown_scenarios)}")
    return config


def _default_output():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return HERE / f"baseline-{stamp}.json"


async def run_baseline(config, *, output, base_url=None, db_base_url=None,
                       server_info=None, server_host=None):
    """Run the configured baseline and write the JSON file. Returns the dict."""
    selected = [s for s in SCENARIOS if s["name"] in config["scenarios"]]
    modes = list(config["modes"])
    external = base_url is not None
    limitations = []
    if external:
        limitations.append(
            "external server: host separation is unverified; server "
            "environment comes from --server-info when given")
        if server_info is None:
            limitations.append(
                "no --server-info given: server hardware/software is unknown, "
                "so this baseline cannot be reproduced from this file alone")
    else:
        limitations.append(
            "same-machine run: the load generator shares CPU and memory with "
            "the server, so throughput is a regression baseline, not a "
            "production throughput claim")
    if not config["production"]:
        limitations.append("development mode: SSG/ISR pages are rendered on "
                            "demand instead of served from a build manifest")
    if config["repetitions"] == 1:
        limitations.append("single repetition: latency percentiles have no "
                            "cross-run median; re-run with repetitions >= 3 "
                            "for a publishable baseline")

    environment = {"load_generator": collect_environment(ROOT)}
    if server_info is not None:
        with open(server_info) as handle:
            environment["server"] = json.load(handle)
    elif external:
        environment["server"] = {"host": server_host, "environment": "unknown (no --server-info)"}
    else:
        environment["server"] = environment["load_generator"]

    conditions = {
        "requests_per_scenario_rep": config["requests"],
        "concurrency": config["concurrency"],
        "warmup_requests": config["warmup"],
        "repetitions": config["repetitions"],
        "production": config["production"],
        "modes": modes,
        "server_workers": 1 if not external else "unknown (external server; see server-info)",
        "transport": "external_http (separate server process)" if not external
        else "external_http (host separation unverified)",
        "loadgen_location": "external-unverified" if external else "same-machine",
        "server_host": server_host,
        "config_file": str(config.get("_config_path", DEFAULT_CONFIG)),
    }

    scenarios_out = []
    skipped = []
    for scenario in selected:
        entry = {"name": scenario["name"], "path": scenario["path"],
                 "description": scenario["description"], "app": scenario["app"],
                 "results": {}}
        for mode in modes:
            if mode == "starlette" and scenario["name"] not in STARLETTE_SCENARIOS:
                skipped.append({"scenario": scenario["name"], "mode": mode,
                                "reason": "Starlette reference app does not implement this route"})
                continue
            if external and scenario["app"] == "db" and db_base_url is None:
                skipped.append({"scenario": scenario["name"], "mode": mode,
                                "reason": "external run without --db-base-url: "
                                "no DB fixture server to measure"})
                continue
            repetitions = []
            for _ in range(config["repetitions"]):
                if external:
                    url = db_base_url if scenario["app"] == "db" else base_url
                    async with httpx.AsyncClient(base_url=url, timeout=30,
                                                 trust_env=False) as client:
                        result = await _measure_external(client, scenario, config)
                else:
                    result = await _measure_local(scenario, mode, config)
                repetitions.append(result)
            entry["results"][mode] = {"repetitions": repetitions,
                                      "aggregate": aggregate(repetitions)}
        scenarios_out.append(entry)

    baseline = {
        "baseline": "pynext-c1",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conditions": conditions,
        "environment": environment,
        "limitations": limitations,
        "scenarios": scenarios_out,
        "skipped": skipped,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, indent=2) + "\n")
    return baseline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", help="baseline JSON path (default: benchmarks/baseline-<UTC stamp>.json)")
    parser.add_argument("--smoke", action="store_true",
                        help="tiny run for script verification: 50 requests, concurrency 4, "
                        "1 repetition, axum only, development mode")
    parser.add_argument("--requests", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--modes", nargs="+", choices=MODES)
    parser.add_argument("--scenarios", nargs="+", choices=[s["name"] for s in SCENARIOS])
    parser.add_argument("--production", dest="production", action="store_true", default=None)
    parser.add_argument("--no-production", dest="production", action="store_false")
    parser.add_argument("--base-url", help="measure an already-running server (separate machine)")
    parser.add_argument("--db-base-url", help="already-running DB fixture server (external runs only)")
    parser.add_argument("--server-info", help="environment JSON from benchmarks/collect_env.py on the server machine")
    parser.add_argument("--server-host", help="label for the server host in external runs")
    args = parser.parse_args()

    config = _load_config(args.config)
    config["_config_path"] = args.config
    for key in ("requests", "concurrency", "warmup", "repetitions", "modes",
                "scenarios", "production"):
        value = getattr(args, key)
        if value is not None:
            config[key] = value
    if args.smoke:
        config.update({"requests": 50, "concurrency": 4, "warmup": 5,
                       "repetitions": 1, "modes": ["axum"], "production": False})
    if not args.base_url and config["production"]:
        built = ensure_build()
        if built:
            print(built, file=sys.stderr)

    output = args.output or _default_output()
    baseline = asyncio.run(run_baseline(
        config, output=output, base_url=args.base_url, db_base_url=args.db_base_url,
        server_info=args.server_info, server_host=args.server_host))
    print(json.dumps({
        "output": str(output),
        "scenarios": [s["name"] for s in baseline["scenarios"]],
        "modes": baseline["conditions"]["modes"],
        "limitations": baseline["limitations"],
    }, indent=2))


if __name__ == "__main__":
    main()
