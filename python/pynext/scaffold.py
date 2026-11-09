"""Create a small, runnable PyNext application without overwriting files."""
from pathlib import Path

TEMPLATES = ("basic", "islands", "api")

_GITIGNORE = """\
.pynext/
__pycache__/
*.pyc
"""

_BASIC_PAGE = '''from pynext import h, page


@page()
def Page():
    return h("main", {}, h("h1", {}, "Hello PyNext!"))
'''

_ISLANDS_PAGE = '''from pynext import h, island, page


@page()
def Page():
    return h("main", {},
             h("h1", {}, "Hello PyNext!"),
             h("p", {}, "This counter runs in the browser as an island."),
             island("counter", props={"initial": 0}, hydrate=True,
                    fallback=h("button", {"type": "button"}, "Count: 0")))
'''

_API_PAGE = '''from pynext import h, page


@page()
def Page():
    return h("main", {},
             h("h1", {}, "Hello PyNext!"),
             h("p", {}, h("a", {"href": "/api/hello"}, "Try the example API route.")))
'''

_COUNTER_ISLAND = '''type Props = { initial: number };

// A hydrated island must export render, hydrate and mount: render builds
// the browser DOM, hydrate attaches behaviour to the server-rendered
// fallback, and mount is the non-hydrated fallback path.
export function render(props: Props): Node {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = `Count: ${props.initial}`;
  return button;
}

export function hydrate(element: HTMLElement, props: Props) {
  const button = element.querySelector("button");
  if (!button) throw new Error("Expected a server-rendered button");
  let count = props.initial;
  const onClick = () => {
    count += 1;
    button.textContent = `Count: ${count}`;
  };
  button.addEventListener("click", onClick);
  return () => button.removeEventListener("click", onClick);
}

export function mount(element: HTMLElement, props: Props) {
  element.replaceChildren(render(props));
  return hydrate(element, props);
}
'''

_API_ROUTE = '''from pynext import JSONResponse, route


@route("GET")
async def hello():
    return JSONResponse({"message": "Hello from PyNext!"})
'''

_MIDDLEWARE = '''import time


async def timing(request, call_next):
    """Example middleware: time the request and report it in a header."""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers.append((b"x-response-time-ms", f"{elapsed_ms:.1f}".encode("ascii")))
    return response


MIDDLEWARE = [timing]
'''

_README = {
    "basic": '''# PyNext app

Run `pynext dev` from this directory. To build and run the production
version, use `pynext build` followed by `pynext start`.

## Next steps

- `pynext dev` — serve the source with reload
- `pynext build` — render static pages and bundle islands
- `pynext start` — serve the production build
''',
    "islands": '''# PyNext app (islands template)

Run `pynext dev` from this directory. To build and run the production
version, use `pynext build` followed by `pynext start`.

`islands/counter.ts` is a browser island: the button on the home page is
rendered on the server and hydrated in the browser. Islands need Node.js
and esbuild for `pynext build`.

## Next steps

- `pynext dev` — serve the source with reload
- `pynext build` — render static pages and bundle islands
- `pynext start` — serve the production build
''',
    "api": '''# PyNext app (api template)

Run `pynext dev` from this directory. To build and run the production
version, use `pynext build` followed by `pynext start`.

`app/api/hello/route.py` is an example JSON route — try
`curl http://127.0.0.1:3000/api/hello` while the server is running.

## Next steps

- `pynext dev` — serve the source with reload
- `pynext build` — render static pages and bundle islands
- `pynext start` — serve the production build
''',
}

_TEMPLATE_FILES = {
    "basic": {"app/page.py": _BASIC_PAGE},
    "islands": {"app/page.py": _ISLANDS_PAGE, "islands/counter.ts": _COUNTER_ISLAND},
    "api": {"app/page.py": _API_PAGE, "app/api/hello/route.py": _API_ROUTE},
}


def init_project(root, template="basic", with_middleware=False, gitignore=True):
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; choose one of {', '.join(TEMPLATES)}")
    root = Path(root).resolve()
    files = dict(_TEMPLATE_FILES[template])
    files["README.md"] = _README[template]
    if gitignore:
        files[".gitignore"] = _GITIGNORE
    if with_middleware:
        files["middleware.py"] = _MIDDLEWARE
    app_dir = root / "app"
    if app_dir.is_symlink() or (app_dir.exists() and not app_dir.is_dir()):
        raise ValueError("app must be a directory inside the project")
    for relative in files:
        target = root / relative
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Refusing to overwrite {target}")
    # Create missing ancestors individually so cleanup owns exactly the
    # directories this run created, including intermediate project parents.
    missing_parents = []
    current = root
    while not current.exists():
        missing_parents.append(current)
        current = current.parent
    created = []
    created_dirs = []
    # Every directory the files need, shallowest first so parents exist
    # before children; intermediate parents (e.g. app/api/hello) are
    # tracked here so a failed run can remove them again.
    wanted = {app_dir, root / "public"}
    if template == "islands":
        wanted.add(root / "islands")
    for relative in files:
        parent = (root / relative).parent
        while parent != root:
            wanted.add(parent)
            parent = parent.parent
    directories = [*reversed(missing_parents),
                   *sorted(wanted, key=lambda p: len(p.parts))]
    try:
        for directory in directories:
            try:
                directory.mkdir()
            except FileExistsError:
                if directory.is_symlink() or not directory.is_dir():
                    raise
            else:
                created_dirs.append(directory)
        for relative, content in files.items():
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
