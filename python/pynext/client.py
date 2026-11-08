"""Build isolated browser modules and a versioned client manifest."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .assets import _read_public, _validate_component
from .components import _ISLAND_NAME

PREFIX = "/_pynext/static/"
MAX_SOURCE_BYTES = 1 * 1024 * 1024
MAX_SOURCE_TOTAL = 10 * 1024 * 1024
MAX_SOURCE_FILES = 10_000
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_TOTAL = 32 * 1024 * 1024
MAX_ENTRIES = 256
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _runtime_bytes():
    return (Path(__file__).parent / "browser" / "runtime.js").read_bytes()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _entries(sources):
    result = {}
    for relative in sources:
        path = Path(relative)
        if len(path.parts) == 1:
            if not _ISLAND_NAME.fullmatch(path.stem):
                raise ValueError(f"Invalid island name: {path.stem}")
            if path.stem in result:
                raise ValueError(f"Duplicate island name: {path.stem}")
            result[path.stem] = relative
    if sources and not result:
        raise ValueError("islands must include at least one top-level .js or .ts entry")
    if len(result) > MAX_ENTRIES:
        raise ValueError("Too many island entries")
    return dict(sorted(result.items()))


def discover_client(root):
    directory = root / "islands"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("islands must be a directory, not a symbolic link")
    if not directory.exists():
        return {}
    result, total = {}, 0

    def fail(exc):
        raise ValueError("Cannot scan islands directory") from exc

    for current, dirs, files in os.walk(directory, followlinks=False, onerror=fail):
        for name in dirs + files:
            _validate_component(name)
            entry = Path(current) / name
            if entry.is_symlink():
                raise ValueError(f"Island symlink is not allowed: {entry}")
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix not in {".js", ".ts"}:
                raise ValueError(f"Island sources must be .js or .ts: {path}")
            relative = path.relative_to(directory)
            data = _read_public(root, relative.parts, source_dir="islands")
            total += len(data)
            if len(data) > MAX_SOURCE_BYTES or total > MAX_SOURCE_TOTAL or len(result) >= MAX_SOURCE_FILES:
                raise ValueError("Island source count or size limit exceeded")
            result[relative.as_posix()] = _digest(data)
        dirs.sort()
    result = dict(sorted(result.items()))
    _entries(result)
    return result


def _esbuild(root):
    explicit = os.environ.get("PYNEXT_ESBUILD")
    if explicit:
        path = Path(explicit)
        candidate = path if path.is_absolute() else root / path
        executable = shutil.which(explicit) or (str(candidate.resolve()) if candidate.is_file() else None)
        if not executable or not os.access(executable, os.X_OK):
            raise ValueError("PYNEXT_ESBUILD does not point to an executable")
        if sys.platform == "win32" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
            raise ValueError("PYNEXT_ESBUILD must point to a native esbuild executable on Windows")
        return [str(executable)]
    node = shutil.which("node") if sys.platform == "win32" else None
    for parent in (root, *root.parents):
        if sys.platform == "win32":
            wrapper = parent / "node_modules" / "esbuild" / "bin" / "esbuild"
            if node and wrapper.is_file():
                return [node, str(wrapper)]
        else:
            candidate = parent / "node_modules" / ".bin" / "esbuild"
            if candidate.is_file():
                return [str(candidate)]
    executable = shutil.which("esbuild")
    if not executable:
        raise ValueError("esbuild is required for islands; install esbuild in the project or set PYNEXT_ESBUILD")
    if sys.platform == "win32" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        parent = Path(executable).parent
        wrappers = (parent.parent / "esbuild" / "bin" / "esbuild",
                    parent / "node_modules" / "esbuild" / "bin" / "esbuild")
        for wrapper in wrappers:
            if node and wrapper.is_file():
                return [node, str(wrapper)]
        raise ValueError("Set PYNEXT_ESBUILD to a native esbuild executable on Windows")
    return [executable]


def compile_client(root, sources):
    entries = _entries(sources)
    runtime = _runtime_bytes()
    runtime_hash = _digest(runtime)
    assets = {PREFIX + runtime_hash + ".js": runtime}
    if not entries:
        return {"runtime": runtime_hash, "bundles": {}, "hydration": {}, "esbuild_version": None}, assets
    executable = _esbuild(root)
    try:
        version = subprocess.run([*executable, "--version"], check=True, encoding="utf-8", errors="replace",
                                 capture_output=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Cannot run esbuild") from exc
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Unexpected esbuild version")
    directory = (root / "islands").resolve()
    bundles = {}
    hydration = {}
    total = len(runtime)
    with tempfile.TemporaryDirectory(prefix="pynext-client-") as temp:
        output = Path(temp) / "bundle.js"
        metadata = Path(temp) / "meta.json"
        for name, entry in entries.items():
            command = [*executable, entry, "--bundle", "--format=esm", "--platform=browser", "--target=es2020",
                       "--charset=utf8", "--log-level=error", "--log-override:unsupported-dynamic-import=error",
                       f"--outfile={output}", f"--metafile={metadata}"]
            try:
                subprocess.run(command, cwd=directory, check=True, encoding="utf-8", errors="replace",
                               capture_output=True, timeout=30)
            except subprocess.CalledProcessError as exc:
                raise ValueError(f"Could not bundle island {name}: {exc.stderr[:4000]}") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Could not bundle island {name}") from exc
            info = json.loads(metadata.read_text(encoding="utf-8"))
            for source in info["inputs"]:
                resolved = (directory / source).resolve()
                if not resolved.is_relative_to(directory) or resolved.relative_to(directory).as_posix() not in sources:
                    raise ValueError(f"Island {name} imports outside the islands directory: {source}")
            outputs = list(info["outputs"].values())
            if len(outputs) != 1 or outputs[0].get("imports") or "mount" not in outputs[0].get("exports", []):
                raise ValueError(f"Island {name} must bundle to one module with a named mount export and no external imports")
            data = output.read_bytes()
            total += len(data)
            if len(data) > MAX_BUNDLE_BYTES or total > MAX_BUNDLE_TOTAL:
                raise ValueError("Island bundle size limit exceeded")
            digest = _digest(data)
            bundles[name] = digest
            hydration[name] = {"render", "hydrate"}.issubset(outputs[0].get("exports", []))
            assets[PREFIX + digest + ".js"] = data
    return {"runtime": runtime_hash, "bundles": bundles, "hydration": hydration, "esbuild_version": version}, assets


def read_client_artifacts(root, manifest):
    entries = _entries(manifest["client_sources"])
    info = manifest.get("client")
    if not isinstance(info, dict) or set(info) != {"runtime", "bundles", "hydration", "esbuild_version"}:
        raise ValueError("Invalid client manifest; rebuild the application")
    if not entries and info != {"runtime": _digest(_runtime_bytes()), "bundles": {},
                                "hydration": {}, "esbuild_version": None}:
        raise ValueError("Invalid empty client manifest; rebuild the application")
    if (not isinstance(info["bundles"], dict) or set(info["bundles"]) != set(entries) or
            not isinstance(info["hydration"], dict) or set(info["hydration"]) != set(entries) or
            any(not isinstance(value, bool) for value in info["hydration"].values()) or
            (bool(entries) and (not isinstance(info["esbuild_version"], str) or
             not re.fullmatch(r"\d+\.\d+\.\d+", info["esbuild_version"]))) or
            info["runtime"] != _digest(_runtime_bytes())):
        raise ValueError("Incompatible client manifest; rebuild the application")
    if any(not isinstance(digest, str) or not _DIGEST.fullmatch(digest) for digest in info["bundles"].values()):
        raise ValueError("Invalid client bundle digest")
    directory = root / ".pynext" / "static"
    assets = {}
    total = 0
    for digest in {info["runtime"], *info["bundles"].values()}:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid client bundle digest")
        file = directory / (digest + ".js")
        if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
            raise ValueError("Missing client bundle; rebuild the application")
        with file.open("rb") as source:
            data = source.read(MAX_BUNDLE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_BUNDLE_BYTES or total > MAX_BUNDLE_TOTAL or _digest(data) != digest:
            raise ValueError("Client bundle failed integrity check; rebuild the application")
        assets[PREFIX + digest + ".js"] = data
    return info, assets
