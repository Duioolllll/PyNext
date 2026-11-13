"""Environment diagnostics for `pynext info` / `pynext doctor`.

`info` prints the environment a bug report needs (PyNext/Python/esbuild
versions, native extension, project manifest); `doctor` prints the same
lines plus a pass/fail check list and exits non-zero when a required
component is missing.
"""
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from .build import MANIFEST, read_build_report

CI_VARIABLES = (
    ("GITHUB_ACTIONS", "GitHub Actions"),
    ("GITLAB_CI", "GitLab CI"),
    ("CIRCLECI", "CircleCI"),
    ("TRAVIS", "Travis CI"),
    ("JENKINS_URL", "Jenkins"),
    ("TEAMCITY_VERSION", "TeamCity"),
    ("CI", "CI"),
)


def detect_ci():
    """Return the CI name when running under a known CI, else None."""
    for variable, name in CI_VARIABLES:
        if os.environ.get(variable):
            return name
    return None


def pynext_version():
    try:
        return importlib.metadata.version("pynext-framework")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def esbuild_info(root):
    """Return ``(path, version)`` for the esbuild binary, or ``(None, None)``."""
    from .client import _esbuild
    try:
        command = _esbuild(Path(root))
    except ValueError:
        return None, None
    try:
        version = subprocess.run([*command, "--version"], check=True, encoding="utf-8",
                                 errors="replace", capture_output=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    return command[-1], version


def manifest_info(root):
    """Summarise the build manifest without validating sources.

    Diagnostics only: a stale manifest (sources changed since build)
    is still informative here, unlike `load_manifest`, which rejects it.
    """
    target = Path(root) / MANIFEST
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"present": False}
    if not isinstance(saved, dict):
        return {"present": False}
    routes = saved.get("routes")
    modes = saved.get("render_modes") or {}
    counts = {"total": 0, "ssg": 0, "isr": 0, "ssr": 0, "api": 0}
    if isinstance(routes, dict):
        counts["total"] = len(routes)
        for pattern, spec in routes.items():
            if isinstance(spec, dict) and spec.get("kind") == "route":
                counts["api"] += 1
            else:
                # A stale or hand-edited manifest may carry a render
                # mode this version does not know; diagnostics should
                # still print, counting the route as SSR.
                mode = modes.get(pattern, "ssr")
                counts[mode if mode in ("ssg", "isr", "ssr") else "ssr"] += 1
    return {"present": True, "schema_version": saved.get("schema_version"), "routes": counts}


def collect_info(root):
    """Collect the diagnostics for a project root."""
    root = Path(root).resolve()
    try:
        from . import _native
        native = {"loaded": True, "path": getattr(_native, "__file__", None)}
    except ImportError:
        native = {"loaded": False, "path": None}
    islands = root / "islands"
    has_islands = bool(islands.is_dir() and any(
        path.is_file() and path.suffix in {".js", ".ts"} for path in islands.rglob("*")))
    esbuild_path, esbuild_version = esbuild_info(root)
    manifest = manifest_info(root)
    report = read_build_report(root)
    info = {
        "pynext_version": pynext_version(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "native": native,
        "esbuild_path": esbuild_path,
        "esbuild_version": esbuild_version,
        "root": str(root),
        "has_islands": has_islands,
        "manifest": manifest,
        "build_report": report,
        "ci": detect_ci(),
    }
    checks = [
        {"label": "native extension", "ok": native["loaded"],
         "detail": "loaded" if native["loaded"] else "missing — rebuild the native extension"},
        {"label": "esbuild", "ok": esbuild_version is not None or not has_islands,
         "detail": (f"{esbuild_version}" if esbuild_version is not None else
                    "not found (no islands, not needed)" if not has_islands else
                    "not found — install esbuild to build islands")},
        {"label": "build manifest", "ok": manifest["present"],
         "detail": "present" if manifest["present"] else "absent — run `pynext build` first"},
    ]
    info["checks"] = checks
    return info


def format_info(info, *, doctor=False):
    """Format collected diagnostics as printable lines."""
    native = info["native"]
    if native["loaded"]:
        native_line = "loaded" + (f" ({native['path']})" if native["path"] else "")
    else:
        native_line = "missing — rebuild the native extension"
    if info["esbuild_version"] is not None:
        esbuild_line = f"{info['esbuild_version']} ({info['esbuild_path']})"
    elif not info["has_islands"]:
        esbuild_line = "not found (no islands, not needed)"
    else:
        esbuild_line = "not found — install esbuild to build islands"
    manifest = info["manifest"]
    if manifest["present"]:
        counts = manifest["routes"]
        manifest_line = (f"present (schema {manifest['schema_version']}, {counts['total']} routes: "
                         f"{counts['ssg']} SSG, {counts['isr']} ISR, {counts['ssr']} SSR, "
                         f"{counts['api']} API)")
    else:
        manifest_line = "absent — run `pynext build` first"
    report = info["build_report"]
    if report is not None:
        pages = report["static_pages"]
        report_line = (f"present ({report['duration_seconds']}s, "
                       f"{pages['rendered']} rendered / {pages['reused']} reused static pages)")
    else:
        report_line = "absent — run `pynext build`"
    lines = [
        f"PyNext {info['pynext_version']}",
        f"Python {info['python_version']} ({info['python_executable']})",
        f"Platform: {info['platform']}",
        f"Native extension: {native_line}",
        f"esbuild: {esbuild_line}",
        f"Project: {info['root']}",
        f"Manifest: {manifest_line}",
        f"Build report: {report_line}",
        f"CI: {info['ci'] or 'no'}",
    ]
    if doctor:
        failed = [check for check in info["checks"] if not check["ok"]]
        lines.append("")
        lines.append(f"Checks: {len(info['checks']) - len(failed)} passed, {len(failed)} failed")
        for check in info["checks"]:
            status = "ok" if check["ok"] else "FAIL"
            lines.append(f"  [{status}] {check['label']}: {check['detail']}")
    return "\n".join(lines) + "\n"
