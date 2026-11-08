"""Collect the machine/software environment for a reproducible baseline.

Run this on the server machine and on the load-generator machine, and pass
the server file to ``benchmarks/baseline.py --server-info`` so the baseline
JSON records both sides. The output is facts only: no measurement happens
here, so the same file can be reused for every run on the same machines
until something changes.

Usage:
    python benchmarks/collect-env.py [--output server-env.json]
"""
import argparse
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _cpu_model():
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or None


def _command_version(command):
    try:
        result = subprocess.run([command, "--version"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0].strip() if output else None


def _git_info(repo):
    def run(*args):
        try:
            result = subprocess.run(["git", "-C", str(repo), *args],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None

    status = run("status", "--porcelain")
    return {"sha": run("rev-parse", "HEAD"), "dirty": bool(status)}


def collect(repo):
    try:
        import psutil
        memory = psutil.virtual_memory()
        memory_total = int(memory.total)
    except ImportError:
        memory_total = None
    return {
        "hostname": socket.gethostname(),
        "cpu_model": _cpu_model(),
        "cpu_count_logical": os.cpu_count(),
        "memory_total_bytes": memory_total,
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pynext": _package_version("pynext-framework"),
        "uvicorn": _package_version("uvicorn"),
        "starlette": _package_version("starlette"),
        "httpx": _package_version("httpx"),
        "psutil": _package_version("psutil"),
        "rustc": _command_version("rustc"),
        "cargo": _command_version("cargo"),
        "node": _command_version("node"),
        "git": _git_info(repo),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="write JSON here instead of stdout")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    env = collect(repo)
    text = json.dumps(env, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
