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
