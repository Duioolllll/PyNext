# Deployment and security (experimental)

[Home](Home.md) · [한국어](../ko/Deployment-and-Security.md)

**Do not treat PyNext 0.1.0 as a hardened public production server.** The project explicitly warns against direct internet exposure and execution of untrusted application code.

## Building and serving trusted local apps

~~~bash
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic --host 127.0.0.1 --port 3000
~~~

`start` validates the manifest and hashes of tracked Python, public and island source inputs, plus generated asset integrity. **It does not track every external dependency, environment variable or data source**. Rebuild after relevant changes. The build itself imports and executes developer-provided Python modules; never build unknown apps in a privileged environment.

The app source's `public/` and `islands/` directories are still required at production startup unless you deploy the Linux standalone directory (`pynext build --dir <app> --standalone`), which bundles the interpreter, installed dependencies and built application into `<app>/.pynext/standalone` (compatible system libraries are still required).

### Standalone bundle contents

The bundle root holds a `bundle-manifest.json` recording the bundle's provenance: manifest schema `version`, `pynext_version`, `python_version`, a deterministic `build_id` (derived from the source hashes when the build manifest records none), `esbuild_version`, the copied third-party packages with versions (`copied_packages`), packages left out with `--standalone-exclude` (`excluded_packages`), symlinked files dropped during the copy (`dropped_symlinks`, bundle-relative paths), the environment files copied in (`env_files`), and the total file count and bytes. The bundle also copies the project root's `.env` and `.env.production` into the bundled app directory; `.env.local` and `.env.*.local` are never copied. When the bundled server starts it logs the bundle's PyNext/Python versions, build id, package count and size, and warns if the bundle was built with a different Python or PyNext version than the one now running it. `--standalone-exclude <name>` (repeatable) omits a top-level site-packages entry from the bundle; `pynext` itself cannot be excluded.

## External ASGI server

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

That included entry point creates a **development-mode** app. For built SSG behavior, create an entry point with `create_app(project_path, production=True)`. The ASGI server controls its own timeouts and admission behavior.

## Security boundaries and missing features

- Request body limit is 1 MiB; malformed request data returns 400 where validated.
- Response CSP defaults to `script-src 'self'; object-src 'none'`.
- Island props are public and must not contain secrets.
- ISR pages are public cached content; `Cookie`/`Authorization` bypass shared caching.
- Protect any custom revalidation endpoint with real authorization.
- Python synchronous work cannot be forcibly stopped at a request timeout. Use DB timeouts and idempotent operations.
- WebSockets, structured logging/metrics/OpenTelemetry, shared worker caches (SQLite/Redis) and process isolation via the supervisor are implemented. Global connection limits are implemented on the native server (total cap and per-IP cap, plus header-read and slow-body timeouts; see `serve()`/`start_native()`).
- Avoid relying on unvalidated reverse-proxy headers or calling the HTTP/2 h2c implementation a full TLS termination solution. TLS termination belongs to the reverse proxy; see [ADR-0001](../../adr/0001-tls-termination.md).

For production hardening ideas and acceptance criteria, see [production roadmap](../../PRODUCTION_ROADMAP.md). The roadmap is **not a list of implemented functionality**.
