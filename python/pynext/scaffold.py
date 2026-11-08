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
    root.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(exist_ok=True)
    created = []
    try:
        for relative, content in _FILES.items():
            target = root / relative
            with target.open("x", encoding="utf-8", newline="\n") as output:
                created.append(target)
                output.write(content)
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return root
