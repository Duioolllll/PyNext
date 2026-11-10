"""Build a relocatable Linux application directory from the current environment."""
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import sysconfig
from functools import partial
from pathlib import Path

from . import __version__ as PYNEXT_VERSION

log = logging.getLogger("pynext")

# Schema version of bundle-manifest.json. Bump when its fields change.
BUNDLE_MANIFEST_VERSION = 1
BUNDLE_MANIFEST = "bundle-manifest.json"
# Environment files copied into the bundle (Next.js copies .env and
# .env.production only). Local overrides are never shipped: they may hold
# development secrets.
ENV_FILES = (".env", ".env.production")


def _project_copy(source, target):
    if source.is_symlink():
        raise ValueError(f"Standalone project input must not be a symlink: {source}")
    if source.is_dir():
        for current, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for name in dirs + files:
                if (Path(current) / name).is_symlink():
                    raise ValueError(f"Standalone project input must not be a symlink: {Path(current) / name}")
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    elif source.is_file():
        shutil.copy2(source, target)


def _ignore_runtime(directory, names, *, warn=False, dropped=None, src_root=None, dst_root=None):
    base = Path(directory)
    ignored = {name for name in names if (base / name).is_symlink() or name == "__pycache__" or
               name.endswith((".pyc", ".pth")) or name in {"sitecustomize.py", "usercustomize.py"}}
    if dropped is not None:
        # Record every symlinked item dropped from the bundle so the
        # bundle manifest names it (a bundle missing a dependency it
        # needs must be diagnosable, not silently incomplete). When the
        # copy roots are known, record the destination path inside the
        # bundle; otherwise fall back to the source path.
        for name in sorted(ignored):
            if not (base / name).is_symlink():
                continue
            if src_root is not None and dst_root is not None:
                dropped.append(str(Path(dst_root) / base.relative_to(src_root) / name))
            else:
                dropped.append(str(base / name))
    if warn:
        # Nested symlinks are dropped, not rejected: this ignore function
        # is shared with the stdlib copy, where a symlink is not
        # necessarily a problem, so rejecting here could break builds on
        # environments whose stdlib links files. A warning names the
        # dropped item so a bundle missing a dependency it needs is
        # diagnosable instead of silently incomplete.
        for name in sorted(ignored):
            if (base / name).is_symlink():
                log.warning("Standalone bundle drops symlinked item (unsupported): %s", base / name)
    return ignored


def _dist_info_packages(purelib):
    """Map top-level site-packages entries to their distribution.

    Reads each ``*.dist-info`` directory's METADATA (name/version) and
    RECORD (installed file list, whose first path component is the
    top-level entry) plus top_level.txt when present. Entries with no
    distribution metadata are reported with ``version`` None by the
    caller instead of being guessed at.
    """
    mapping = {}
    for dist_info in sorted(purelib.glob("*.dist-info"), key=lambda p: p.name):
        metadata = dist_info / "METADATA"
        name = version = None
        try:
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                if name and version:
                    break
        except OSError:
            continue
        if not name:
            continue
        entries = set()
        top_level = dist_info / "top_level.txt"
        try:
            entries.update(line.strip() for line in
                           top_level.read_text(encoding="utf-8", errors="replace").splitlines()
                           if line.strip())
        except OSError:
            pass
        record = dist_info / "RECORD"
        try:
            for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
                path = line.split(",", 1)[0].strip()
                if not path or path.startswith(".."):
                    continue
                first = path.split("/", 1)[0]
                if first and not first.endswith(".dist-info"):
                    entries.add(first)
        except OSError:
            pass
        if not entries:
            # Fall back to the normalized distribution name (covers
            # single-module distributions whose RECORD is absent).
            entries.add(re.sub(r"[-_.]+", "_", name).lower())
        for entry in entries:
            mapping.setdefault(entry, {"name": name, "version": version})
    return mapping


def _copy_site_packages(purelib, packages, *, excludes=frozenset(), dropped=None):
    """Copy installed third-party packages into the bundle, in sorted order.

    Symlinked top-level entries are rejected, like .pth files: a bundle
    cannot carry a link to a path outside itself, and silently dropping
    the entry would ship a bundle missing a dependency it needs.
    Symlinked items nested inside a package are dropped with a warning
    (see _ignore_runtime): the package still copies, but the missing
    file is visible in the build log and recorded in ``dropped``.
    Entries in ``excludes`` are skipped entirely and reported back.

    Returns {"copied": [{"name", "version"}], "excluded": [names]}.
    ``copied`` covers top-level modules/packages only (``*.dist-info``
    directories are copied as files but are metadata, not packages).
    """
    dist_info = _dist_info_packages(purelib)
    copied = []
    excluded = []
    for item in sorted(purelib.iterdir(), key=lambda entry: entry.name):
        if item.is_symlink():
            raise ValueError(f"Standalone does not support symlinked dependencies: {item.name}")
        if (item.name.startswith(".") or item.name.endswith((".pth", ".pyc")) or
                item.name == "__pycache__"):
            continue
        # An exclude names a package or module ("plain" excludes plain.py);
        # record the name as requested, not the on-disk entry name.
        matched = next((name for name in (item.name, item.stem) if name in excludes), None)
        if matched is not None:
            excluded.append(matched)
            continue
        target = packages / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True,
                            ignore=partial(_ignore_runtime, warn=True, dropped=dropped,
                                           src_root=item, dst_root=target))
        elif item.is_file():
            shutil.copy2(item, target)
        if item.name.endswith((".dist-info", ".egg-info")):
            # Distribution metadata is copied as files but is not a package.
            continue
        if item.name == "pynext":
            # The bundled pynext comes from the running source tree (the
            # overlay below replaces this copy); its version is recorded
            # as pynext_version, not as a copied package.
            continue
        info = (dist_info.get(item.name) or dist_info.get(item.stem)
                or {"name": item.stem, "version": None})
        copied.append({"name": info["name"], "version": info["version"]})
    known = {entry.name for entry in purelib.iterdir()}
    known.update(entry.stem for entry in purelib.iterdir())
    for name in sorted(set(excludes) - known):
        log.warning("Standalone exclude %s is not installed; nothing to exclude", name)
    return {"copied": copied, "excluded": excluded}


def _build_id(built):
    """Build identifier for the bundle manifest.

    Uses the build manifest's ``build_id`` when a build step recorded
    one; otherwise derives a deterministic id from the sorted source
    hashes (same sources -> same id, so the id is reproducible). The
    ``ad`` substring is avoided so ad blockers cannot mistake a
    build-id-bearing asset URL for an advertisement (Next.js
    generate-build-id does the same).
    """
    if isinstance(built, dict) and isinstance(built.get("build_id"), str) and built["build_id"]:
        return built["build_id"]
    digest = hashlib.sha256()
    for path in sorted(built.get("sources", {})):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(built["sources"][path]).encode("utf-8"))
        digest.update(b"\0")
    base = digest.hexdigest()
    candidate = base[:16]
    counter = 0
    while "ad" in candidate:
        counter += 1
        candidate = hashlib.sha256(f"{base}:{counter}".encode("utf-8")).hexdigest()[:16]
    return candidate


def read_bundle_manifest(app_root):
    """Read the bundle manifest next to a standalone app directory.

    Returns the manifest dict, None when the app is not inside a
    standalone bundle, and raises ValueError when a manifest is present
    but unreadable or of an unknown schema version.
    """
    path = Path(app_root).resolve().parent / BUNDLE_MANIFEST
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Standalone bundle manifest is unreadable: {path}") from exc
    if not isinstance(info, dict) or info.get("version") != BUNDLE_MANIFEST_VERSION:
        raise ValueError(f"Standalone bundle manifest has an unsupported version: {path}")
    return info


def log_bundle_info(app_root):
    """Log a standalone bundle's provenance at server startup.

    No-ops for apps that are not inside a standalone bundle. Warns when
    the bundle was built with a different Python or PyNext version than
    the one now running it, since a bundle is only relocatable across
    machines with a compatible interpreter.
    """
    if app_root is None:
        return
    try:
        info = read_bundle_manifest(app_root)
    except ValueError as exc:
        log.warning("%s", exc)
        return
    if info is None:
        return
    packages = info.get("copied_packages") or []
    files = info.get("files") or 0
    total_bytes = info.get("bytes") or 0
    log.info("Standalone bundle: pynext %s, python %s, build %s, %d packages, %d files (%d bytes)",
             info.get("pynext_version"), info.get("python_version"), info.get("build_id"),
             len(packages), files, total_bytes)
    if info.get("python_version") not in (None, platform.python_version()):
        log.warning("Standalone bundle was built with Python %s but is running on Python %s",
                    info.get("python_version"), platform.python_version())
    if info.get("pynext_version") not in (None, PYNEXT_VERSION):
        log.warning("Standalone bundle was built with PyNext %s but is running PyNext %s",
                    info.get("pynext_version"), PYNEXT_VERSION)


def build_standalone(root, output, *, lock_timeout=None, excludes=()):
    """Create a new bundle; existing output is never overwritten.

    Linux builds include the current interpreter, stdlib, installed Python
    dependencies and the built project. They require a compatible OS/CPU and
    native system libraries; editable third-party packages are unsupported.
    ``excludes`` names top-level site-packages entries to leave out of
    the bundle (recorded in bundle-manifest.json); ``pynext`` itself
    cannot be excluded. A ``bundle-manifest.json`` is written at the
    bundle root recording the Python/PyNext/esbuild versions, the build
    id, the copied packages with versions, and anything dropped or
    excluded, so a bundle can be diagnosed without its build machine.
    """
    if sys.platform != "linux":
        raise ValueError("Standalone directory builds currently support Linux only")
    excludes = tuple(excludes)
    if any(not isinstance(name, str) or not name for name in excludes):
        raise ValueError("Standalone excludes must be non-empty package names")
    if "pynext" in excludes:
        raise ValueError("Standalone cannot exclude the pynext package itself")
    root, output = Path(root).resolve(), Path(output).absolute()
    resolved_output = output.resolve()
    if any(resolved_output.is_relative_to(root / name) for name in ("app", "public", "islands")):
        raise ValueError("Standalone output cannot be inside an application source directory")
    manifest = root / ".pynext" / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Build the project before creating a standalone directory")
    from .build import LOCK_TIMEOUT_SECONDS, BuildLock, load_manifest
    # Serialize against concurrent builds: the copy must see one
    # consistent, completed build, not a manifest mid-rewrite.
    with BuildLock(root, timeout=LOCK_TIMEOUT_SECONDS if lock_timeout is None else lock_timeout):
        built = load_manifest(root)
        return _build_standalone_locked(root, output, built, excludes)


def _build_standalone_locked(root, output, built, excludes=()):
    purelib = Path(sysconfig.get_path("purelib"))
    if purelib == Path(sysconfig.get_path("stdlib")):
        raise ValueError("Build standalone from an isolated Python environment")
    for pth in purelib.glob("*.pth"):
        if pth.name != "pynext_framework.pth":
            raise ValueError(f"Standalone does not support .pth dependencies: {pth.name}")
    package = Path(__file__).resolve().parent
    if not any(package.glob("_native*.so")):
        raise ValueError("PyNext native extension is missing")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Refusing to overwrite {output}")
    # Check every build artifact before creating or copying anything:
    # a missing artifact used to surface only after the runtime and app
    # trees were already copied, leaving a partial bundle behind that
    # blocked the retry with FileExistsError.
    static = root / ".pynext" / "static"
    files = {f"{entry['sha256']}.html" for entry in built["static_pages"].values()}
    files.update(f"{entry['sha256']}.asset" for entry in built["assets"].values())
    client = built["client"]
    files.update(f"{digest}.js" for digest in
                 {client["runtime"], *client["bundles"].values()} if digest is not None)
    for filename in sorted(files):
        if not (static / filename).is_file():
            raise ValueError(f"Missing standalone build artifact: {filename}")
    output.mkdir(parents=True, exist_ok=False)
    try:
        _populate_standalone(root, output, built, purelib, package, excludes)
    except BaseException:
        # Preserve the build exception and make an incomplete cleanup
        # visible so a partial bundle cannot be mistaken for usable output.
        shutil.rmtree(output, ignore_errors=True)
        if output.exists() or output.is_symlink():
            log.warning("Incomplete standalone bundle remains at %s; remove it before retrying", output)
        raise
    return output


def _populate_standalone(root, output, built, purelib, package, excludes=()):
    """Copy the runtime, project and launcher into ``output`` (freshly made)."""
    runtime = output / "runtime"
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = Path(sysconfig.get_path("stdlib"))
    packages = runtime / "lib" / python_version / "site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    # Items dropped from the bundle (nested symlinks) are collected here
    # and recorded in bundle-manifest.json.
    dropped = []
    # Copy installed packages first: symlinked entries are rejected here,
    # so the build fails before the (slow) stdlib copy instead of after it.
    copied = _copy_site_packages(purelib, packages, excludes=frozenset(excludes), dropped=dropped)
    shutil.copytree(stdlib, runtime / "lib" / python_version, dirs_exist_ok=True,
                    ignore=partial(_ignore_runtime, dropped=dropped,
                                   src_root=stdlib, dst_root=runtime / "lib" / python_version))
    (runtime / "bin").mkdir(parents=True)
    shutil.copy2(Path(sys._base_executable).resolve(), runtime / "bin" / "python")
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        for name in sorted({sysconfig.get_config_var("LDLIBRARY"), sysconfig.get_config_var("INSTSONAME")} - {None}):
            lib = Path(libdir) / name
            if lib.is_file():
                shutil.copy2(lib.resolve(), runtime / "lib" / name)
    # The installed copy above may contain files the running source tree
    # no longer has; overlaying it with dirs_exist_ok=True would leave
    # those stale files in the bundle. Replace the directory wholesale
    # so the bundle holds exactly the running source tree.
    pynext_target = packages / "pynext"
    if pynext_target.is_symlink():
        pynext_target.unlink()
    elif pynext_target.exists():
        shutil.rmtree(pynext_target)
    shutil.copytree(package, pynext_target, ignore=partial(_ignore_runtime, warn=True, dropped=dropped,
                                                           src_root=package, dst_root=pynext_target))
    project = output / "app"
    project.mkdir()
    names = {"app", "public", "islands", *(path.name for path in root.glob("*.py"))}
    for name in sorted(names):
        source = root / name
        if source.exists() or source.is_symlink():
            _project_copy(source, project / name)
    # Environment files: .env and .env.production travel with the bundle
    # (Next.js copies the same two). .env.local and .env.*.local never do:
    # they may hold development secrets.
    env_files = []
    for name in ENV_FILES:
        source = root / name
        if source.is_file():
            _project_copy(source, project / name)
            env_files.append(name)
    build = project / ".pynext"
    build.mkdir()
    manifest = root / ".pynext" / "manifest.json"
    _project_copy(manifest, build / "manifest.json")
    # The bundle starts with load_manifest, which requires the BUILD_ID
    # of a completed build; a bundle without it would never start.
    build_id = root / ".pynext" / "BUILD_ID"
    if not build_id.is_file():
        raise ValueError("Missing BUILD_ID; run `pynext build` again")
    _project_copy(build_id, build / "BUILD_ID")
    static = root / ".pynext" / "static"
    files = {f"{entry['sha256']}.html" for entry in built["static_pages"].values()}
    files.update(f"{entry['sha256']}.asset" for entry in built["assets"].values())
    client = built["client"]
    files.update(f"{digest}.js" for digest in
                 {client["runtime"], *client["bundles"].values()} if digest is not None)
    if files:
        (build / "static").mkdir()
        for filename in sorted(files):
            source = static / filename
            if not source.is_file():
                raise ValueError(f"Missing standalone build artifact: {filename}")
            _project_copy(source, build / "static" / filename)
    launcher = output / "run"
    launcher.write_text(
        '#!/bin/sh\nset -eu\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f'export PYTHONHOME="$HERE/runtime" PYTHONNOUSERSITE=1 PYTHONPATH="$HERE/runtime/lib/{python_version}/site-packages"\n'
        'export LD_LIBRARY_PATH="$HERE/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'exec "$HERE/runtime/bin/python" -m pynext.cli start --dir "$HERE/app" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    license_path = Path(f"/usr/share/doc/{python_version}/copyright")
    if license_path.is_file():
        shutil.copy2(license_path, output / "PYTHON-LICENSE")
    _write_bundle_manifest(output, built, copied, dropped, env_files)


def _write_bundle_manifest(output, built, copied, dropped, env_files):
    """Record what the bundle holds and where it came from.

    Written last, after the launcher: ``files``/``bytes`` count the
    bundle without this manifest itself (it does not exist yet when
    counted). No timestamps are recorded, so two builds from the same
    sources produce byte-identical manifests.
    """
    files = [path for path in output.rglob("*") if path.is_file() and not path.is_symlink()]
    info = {
        "version": BUNDLE_MANIFEST_VERSION,
        "pynext_version": PYNEXT_VERSION,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "build_id": _build_id(built),
        "esbuild_version": built.get("client", {}).get("esbuild_version"),
        "copied_packages": sorted(copied["copied"], key=lambda entry: entry["name"].lower()),
        "excluded_packages": sorted(copied["excluded"]),
        "dropped_symlinks": sorted({str(Path(path).relative_to(output)) for path in dropped
                                    if Path(path).is_relative_to(output)}),
        "env_files": sorted(env_files),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
    }
    (output / BUNDLE_MANIFEST).write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
