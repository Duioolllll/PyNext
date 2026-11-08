# Troubleshooting

[Home](Home.md) · [한국어](../ko/Troubleshooting.md)

| Symptom | What to check |
| --- | --- |
| `pynext start` reports source changes | Run `uv run pynext build --dir YOUR_APP` again; verify public/islands files |
| No `.pynext/manifest.json` | Run `pynext build` before `start` |
| Server doesn't pick up code edits | `dev` has **no HMR or reload**; restart |
| `esbuild` missing | Run `npm ci` in repository root, or configure `PYNEXT_ESBUILD` with a real executable |
| Windows custom esbuild path | Point to native `esbuild.exe` rather than a `.cmd` / `.bat` shim |
| Dynamic SSG path returns 404 | Return exactly that path from `generate_static_params()` and rebuild |
| Island cannot be found | Match `island("name")` to `islands/name.js` / `.ts`; rebuild |
| Hydration mismatch | Ensure server fallback matches the JS `render` result; state/focus may be lost |
| API returns 400 | Check invalid UTF-8 query strings, over 1,000 fields, JSON parse failure |
| API returns 413 | Request body exceeds 1 MiB |
| 503 overload | Adjust execution/body capacity after identifying the load; do not disable safeguards |
| 504 timeout | Python handler exceeded deadline; synchronous thread may still run |
| ISR differs between processes | The default `memory` cache is process-local; use `--cache-backend sqlite` or `redis` to share entries across workers |

## Debug checklist

1. Confirm CPython **3.13 or 3.14**, current Cargo/Rust, uv and (if using islands) Node/npm.
2. Verify that `app/page.py` exports a decorated `Page`, that API handlers return a `Response` subclass, and that route paths do not conflict.
3. Run `uv run pytest -q` and `cargo test --locked --workspace --all-features`.
4. Rebuild from trusted sources, restart the server and check one route at a time.
5. Include a minimal reproduction and sanitized traceback when opening an issue. Do not paste API secrets or personal information.

For known limitations, see [Home](Home.md) and [Deployment and security](Deployment-and-Security.md).
