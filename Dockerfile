# syntax=docker/dockerfile:1.7

# ---- build stage --------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies in a separate layer for cache reuse.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime stage ------------------------------------------------------
FROM python:3.15-slim-bookworm AS runtime
WORKDIR /app

RUN groupadd --system --gid 1000 wger \
    && useradd  --system --uid 1000 --gid wger --create-home --home-dir /home/wger wger \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build --chown=wger:wger /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8765

USER wger
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# --transport pinned, not left to MCP_TRANSPORT: this image is an HTTP server
# (it exposes a port and health-checks a listener). A container that picked up
# stdio would find stdin at /dev/null, exit 0 straight away, and be respawned
# forever by `restart: unless-stopped` — a crash loop that reads as success.
CMD ["wger-mcp", "--transport", "http"]
