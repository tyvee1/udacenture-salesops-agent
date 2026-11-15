# Packaging for the SalesOps agent. Build and run outside the course workspace:
#
#   docker build -t udacenture-salesops-agent .
#   docker run --rm --env-file .env udacenture-salesops-agent ask "Summarize the current sales pipeline by stage."
#
# The image never contains .env (see .dockerignore); the key arrives at run time via --env-file.
# Mount logs/ and traces/ to keep run artifacts on the host:
#   docker run --rm --env-file .env -v "$PWD/logs:/app/logs" -v "$PWD/traces:/app/traces" \
#     udacenture-salesops-agent ask "What is the CEO's bonus?"

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM python:3.13-slim

# uv binary from the official image; bytecode compiled at build time; never download another Python.
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    SALESOPS_HOME=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, from the lockfile only, so code changes do not invalidate this layer.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself: code, versioned prompts/config/schemas, eval tasks and the mock data.
COPY src ./src
COPY config ./config
COPY prompts ./prompts
COPY schemas ./schemas
COPY evals ./evals
COPY data ./data
RUN uv sync --frozen --no-dev

# Run as an unprivileged user that owns only the runtime output directories.
RUN useradd --create-home --uid 10001 salesops \
    && mkdir -p logs traces reports state \
    && chown -R salesops:salesops logs traces reports state
USER salesops

ENTRYPOINT ["uv", "run", "--no-sync", "salesops-agent"]
CMD ["--help"]
