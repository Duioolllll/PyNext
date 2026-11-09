"""Build a relocatable Linux application directory from the current environment."""
import logging
import os
import shutil
import sys
import sysconfig
from functools import partial
from pathlib import Path

log = logging.getLogger("pynext")


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


def _ignore_runtime(directory, names, *, warn=False):
    base = Path(directory)
    ignored = {name for name in names if (base / name).is_symlink() or name == "__pycache__" or
               name.endswith((".pyc", ".pth")) or name in {"sitecustomize.py", "usercustomize.py"}}
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


def _copy_site_packages(purelib, packages):
    """Copy installed third-party packages into the bundle, in sorted order.

    Symlinked top-level entries are rejected, like .pth files: a bundle
    cannot carry a link to a path outside itself, and silently dropping
    the entry would ship a bundle missing a dependency it needs.
    Symlinked items nested inside a package are dropped with a warning
    (see _ignore_runtime): the package still copies, but the missing
    file is visible in the build log.
    """
    for item in sorted(purelib.iterdir(), key=lambda entry: entry.name):
        if item.is_symlink():
            raise ValueError(f"Standalone does not support symlinked dependencies: {item.name}")
        if (item.name.startswith(".") or item.name.endswith((".pth", ".pyc")) or
                item.name == "__pycache__"):
            continue
        target = packages / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=partial(_ignore_runtime, warn=True))
        elif item.is_file():
            shutil.copy2(item, target)


def build_standalone(root, output, *, lock_timeout=None):
    """Create a new bundle; existing output is never overwritten.

    Linux builds include the current interpreter, stdlib, installed Python
    dependencies and the built project. They require a compatible OS/CPU and
    native system libraries; editable third-party packages are unsupported.
    """
    if sys.platform != "linux":
        raise ValueError("Standalone directory builds currently support Linux only")
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
        return _build_standalone_locked(root, output, built)


def _build_standalone_locked(root, output, built):
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
        _populate_standalone(root, output, built, purelib, package)
    except BaseException:
        # Preserve the build exception and make an incomplete cleanup
        # visible so a partial bundle cannot be mistaken for usable output.
        shutil.rmtree(output, ignore_errors=True)
        if output.exists() or output.is_symlink():
            log.warning("Incomplete standalone bundle remains at %s; remove it before retrying", output)
        raise
    return output


def _populate_standalone(root, output, built, purelib, package):
    """Copy the runtime, project and launcher into ``output`` (freshly made)."""
    runtime = output / "runtime"
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = Path(sysconfig.get_path("stdlib"))
    packages = runtime / "lib" / python_version / "site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    # Copy installed packages first: symlinked entries are rejected here,
    # so the build fails before the (slow) stdlib copy instead of after it.
    _copy_site_packages(purelib, packages)
    shutil.copytree(stdlib, runtime / "lib" / python_version, dirs_exist_ok=True, ignore=_ignore_runtime)
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
    shutil.copytree(package, pynext_target, ignore=partial(_ignore_runtime, warn=True))
    project = output / "app"
    project.mkdir()
    names = {"app", "public", "islands", *(path.name for path in root.glob("*.py"))}
    for name in sorted(names):
        source = root / name
        if source.exists() or source.is_symlink():
            _project_copy(source, project / name)
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
