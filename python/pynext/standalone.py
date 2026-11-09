"""Build a relocatable Linux application directory from the current environment."""
import os
import shutil
import sys
import sysconfig
from pathlib import Path


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


def _ignore_runtime(directory, names):
    base = Path(directory)
    return {name for name in names if (base / name).is_symlink() or name == "__pycache__" or
            name.endswith((".pyc", ".pth")) or name in {"sitecustomize.py", "usercustomize.py"}}


def _copy_site_packages(purelib, packages):
    """Copy installed third-party packages into the bundle, in sorted order.

    Symlinked top-level entries are rejected, like .pth files: a bundle
    cannot carry a link to a path outside itself, and silently dropping
    the entry would ship a bundle missing a dependency it needs.
    """
    for item in sorted(purelib.iterdir(), key=lambda entry: entry.name):
        if item.is_symlink():
            raise ValueError(f"Standalone does not support symlinked dependencies: {item.name}")
        if (item.name.startswith(".") or item.name.endswith((".pth", ".pyc")) or
                item.name == "__pycache__"):
            continue
        target = packages / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=_ignore_runtime)
        elif item.is_file():
            shutil.copy2(item, target)


def build_standalone(root, output):
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
    from .build import load_manifest
    built = load_manifest(root)
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
    output.mkdir(parents=True, exist_ok=False)
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
    shutil.copytree(package, pynext_target, ignore=_ignore_runtime)
    project = output / "app"
    project.mkdir()
    names = {"app", "public", "islands", *(path.name for path in root.glob("*.py"))}
    for name in sorted(names):
        source = root / name
        if source.exists() or source.is_symlink():
            _project_copy(source, project / name)
    build = project / ".pynext"
    build.mkdir()
    _project_copy(manifest, build / "manifest.json")
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
    return output
