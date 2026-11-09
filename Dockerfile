# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM rust:1-bookworm AS rust
FROM node:22-bookworm-slim AS node

# Compilers, Node and build dependencies stay in this stage.
FROM python:3.13-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
ENV PATH="/usr/local/cargo/bin:${PATH}" \
    RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /src
COPY . .
# Optional CA mount supports managed build proxies without changing image trust.
RUN --mount=type=secret,id=proxy_ca,target=/etc/ssl/certs/ca-certificates.crt \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/usr/local/cargo/registry \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt \
    uv sync --locked --no-dev --no-editable
RUN --mount=type=secret,id=proxy_ca,target=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt npm ci --strict-ssl=true
ARG APP_DIR=examples/basic
RUN uv run --no-sync pynext build --dir "${APP_DIR}" \
    && cp -a "${APP_DIR}" /built-app \
    && chmod -R a+rX /built-app

FROM python:3.13-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Immutable runtime: package installers and their vendored dependencies are
# unnecessary here. Install application dependencies in the build stage.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip \
    && groupadd --gid 10001 pynext \
    && useradd --uid 10001 --gid 10001 --no-create-home pynext
COPY --from=build /opt/venv /opt/venv
COPY --from=build /built-app /app
WORKDIR /app
USER 10001:10001
EXPOSE 3000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:3000/readyz', timeout=2).read()"]
ENTRYPOINT ["python", "-m", "pynext.cli", "start", "--dir", "/app", "--host", "0.0.0.0", "--port", "3000"]
