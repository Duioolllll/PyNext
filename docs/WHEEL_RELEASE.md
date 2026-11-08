# Wheel releases (D1)

PyNext ships as `pynext-framework` with a Rust extension (`pynext._native`),
built with [maturin](https://www.maturin.rs/). Users should only need
`pip install pynext-framework` — no Rust toolchain. This page documents how
the prebuilt wheels are made, verified, and published.

## Build matrix

The `Wheels` workflow (`.github/workflows/wheels.yml`) builds on every push
to `main`, on `v*` tags, on pull requests touching the build inputs, and on
manual dispatch:

| Platform | Target | Python | Notes |
| --- | --- | --- | --- |
| Linux x86_64 | `x86_64-unknown-linux-gnu` | 3.13, 3.14 | manylinux2014 container |
| Linux aarch64 | `aarch64-unknown-linux-gnu` | 3.13, 3.14 | manylinux2014 container + QEMU (cross-built, see limits) |
| macOS | `universal2-apple-darwin` | 3.13, 3.14 | one wheel for arm64 + x86_64 |
| Windows | `x86_64-pc-windows-msvc` | 3.13, 3.14 | x64 only |
| Source | sdist | — | built on Ubuntu, Python 3.13 |

The main `CI` workflow also builds an sdist + wheel per matrix leg
(`uv build`) and runs the same isolated-wheel smoke check; the `Wheels`
workflow is the one that produces the releasable, platform-tagged set.

Each wheel job verifies its wheel before uploading it as an artifact:

1. `tests/smoke_wheel.py` creates a fresh virtualenv, installs **only the
   wheel** (`uv pip install`), and asserts the installed `pynext` comes
   from that venv — no Rust toolchain is used for the install.
2. It runs `pynext build` and starts the built `examples/basic` app from
   the installed wheel and checks the pages over HTTP.
3. Linux jobs additionally require a `manylinux_*` platform tag
   (`PYNEXT_REQUIRE_MANYLINUX=1`); a plain `linux_x86_64` tag fails the
   job, because such a wheel is only installable on the distro it was
   built on.

Artifacts are named `wheel-linux-<target>-py<version>`,
`wheel-macos-universal2-py<version>`, `wheel-windows-x64-py<version>`,
and `sdist`.

## Building locally

```sh
# sdist + wheel for the current platform (needs Rust + cargo on PATH)
uv build --sdist --wheel

# a specific platform tag, e.g. manylinux2014 for the current arch
uv run maturin build --release --compatibility manylinux2014

# cross-compile a Linux aarch64 wheel (needs the target + QEMU/docker)
rustup target add aarch64-unknown-linux-gnu
uv run maturin build --release --target aarch64-unknown-linux-gnu \
  --compatibility manylinux2014
```

Verify a built wheel the same way CI does:

```sh
# one wheel + sdist in dist/ (the default, as in CI)
uv run python tests/smoke_wheel.py

# a single wheel file, no sdist required
PYNEXT_WHEEL=dist/pynext_framework-0.1.0-cp313-cp313-manylinux_2_17_x86_64.whl \
  PYNEXT_SKIP_SDIST=1 uv run python tests/smoke_wheel.py
```

## Publishing to PyPI (manual — CI never uploads)

There is deliberately **no automated PyPI upload** in any workflow.
A release is published by a maintainer, after the `Wheels` run for the
release tag is green:

1. Download the `wheel-*` and `sdist` artifacts from that run into one
   `dist/` directory and re-run the smoke check on each wheel.
2. Attach the wheels + sdist to the GitHub release for the tag.
3. Check name/version availability on PyPI first
   (`https://pypi.org/project/pynext-framework/`).
4. Upload, authenticating with a PyPI API token. PyPI tokens use HTTP
   Basic auth with username `__token__` and the token as the password,
   so the token is stored as the GitHub secret `PY_PI_KEY`
   (project-scoped token for `pynext-framework` is recommended) or as
   local environment variables — never in the repository:

   ```sh
   export TWINE_USERNAME=__token__
   export TWINE_PASSWORD='<token from the PY_PI_KEY secret>'
   uvx twine upload dist/*
   ```

   Trusted publishing (OIDC, no stored token) is the preferred
   alternative once the PyPI project is configured for it; it replaces
   step 4 with a `pypa/gh-action-pypi-publish` step in a release
   workflow, which does not exist yet.

## Limits

- Linux aarch64 wheels are cross-built under QEMU in CI; they pass the
  isolated install + HTTP smoke check on the x86_64 runner, but they
  have not been executed on real aarch64 hardware here. Run the smoke
  check on a Pi 5 / ARM machine before trusting a release for that arch.
- manylinux2014 wheels can only be built inside the manylinux container
  (which the `Wheels` workflow does). A modern distro links glibc
  symbols newer than 2.17 — this machine's glibc 2.39 build links up to
  GLIBC_2.34 — so a local `maturin build --compatibility manylinux2014`
  fails compliance. Do not "fix" this by loosening the compatibility
  flag; the container build is the fix.
- Windows arm64 is not built; only x64.
- macOS wheels require macOS 11+ (universal2). The `macos-latest`
  runner builds both architectures in one wheel.
- Wheels are per-CPython-version (no abi3): a new supported Python
  version needs a new matrix entry in `wheels.yml` and in `ci.yml`.
