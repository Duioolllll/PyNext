"""Create a small, runnable PyNext application without overwriting files."""
from pathlib import Path


_FILES = {
    "app/page.py": '''from pynext import h, page


@page()
def Page():
    return h("main", {}, h("h1", {}, "Hello PyNext!"))
''',
    "README.md": '''# PyNext app

Run `pynext dev` from this directory. To build and run the production
version, use `pynext build` followed by `pynext start`.
''',
}


def init_project(root):
    root = Path(root).resolve()
    app_dir = root / "app"
    if app_dir.is_symlink() or (app_dir.exists() and not app_dir.is_dir()):
        raise ValueError("app must be a directory inside the project")
    for relative in _FILES:
        target = root / relative
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Refusing to overwrite {target}")
    # Remember which directories this run creates so a failure part-way
    # through leaves nothing behind: a failed scaffold used to leave an
    # empty root/app behind (the files were removed, the dirs were not).
    root_existed = root.exists()
    app_existed = app_dir.exists()
    created = []
    created_dirs = []
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not root_existed:
            created_dirs.append(root)
        app_dir.mkdir(exist_ok=True)
        if not app_existed:
            created_dirs.append(app_dir)
        for relative, content in _FILES.items():
            target = root / relative
            with target.open("x", encoding="utf-8", newline="\n") as output:
                created.append(target)
                output.write(content)
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        # Deepest first; only remove dirs this run created, and only when
        # they are empty (a non-empty dir means something else owns it).
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return root
