# One image for the whole app: the logger, the jobs and the web inbox (see compose.yaml).
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    AGENTC_CONFIG=/config/config.toml AGENTC_DATA=/data
# the libraries first, so a code change does not reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENTRYPOINT ["agentc"]
CMD ["--help"]
