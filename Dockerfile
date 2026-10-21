# syntax=docker/dockerfile:1
# One runtime image for the web service and the job worker (they differ only in the command).
#
#   docker build -t hvsr-analyzer --build-arg GIT_COMMIT="$(git rev-parse HEAD)" .
#   docker run --rm -p 8000:8000 hvsr-analyzer                         # web (+ HVSR_EMBEDDED_WORKER)
#   docker run --rm hvsr-analyzer python -m hvsr_web.worker             # separate worker
#   python scripts/smoke.py docker --image hvsr-analyzer                # startup smoke test
#
# Only runtime dependencies from uv.lock are installed: no dev tools and no GPL reference group.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages/hvsr_core/pyproject.toml packages/hvsr_core/README.md packages/hvsr_core/
RUN uv sync --frozen --no-default-groups --no-install-workspace
COPY packages packages
COPY src src
RUN uv sync --frozen --no-default-groups --no-editable

FROM python:3.12-slim-bookworm
ARG GIT_COMMIT=""
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HVSR_GIT_COMMIT=$GIT_COMMIT \
    HVSR_DATABASE_URL=sqlite:////data/hvsr.sqlite3 \
    HVSR_LOCAL_STORE_DIR=/data/objects \
    HVSR_BANK_DIR=/data/banks
RUN useradd --system --uid 10001 --create-home hvsr && mkdir /data && chown hvsr /data
COPY --from=build /opt/venv /opt/venv
COPY scripts/docker-entrypoint.sh /usr/local/bin/hvsr-entrypoint
USER hvsr
WORKDIR /home/hvsr
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/hvsr-entrypoint"]
