# School Register — application image.
#
# Multi-stage build:
#   - "builder" installs the package into a virtualenv
#   - "runtime" copies the venv and the source, nothing else
#
# The result is a small image without build tools or pip caches. This
# is the standard pattern for Python services: it keeps the runtime
# layer minimal, which matters for both security and image pull time.

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Install into a virtualenv. Copying one directory into the runtime
# stage is simpler than trying to copy pip's site-packages and hoping
# nothing was left in system paths.
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /app

# Copy the project metadata AND the package source before installing.
#
# Why source is copied here, not just pyproject.toml:
#   `pip install .` runs setuptools, which reads
#   [tool.setuptools.packages.find] include = ["app*"].
#   If `app/` is absent, setuptools finds no package, the install
#   "succeeds" but produces an empty wheel, and the runtime stage
#   fails at `uvicorn app.main:app` with ModuleNotFoundError.
#
# README.md is required because pyproject.toml declares
# `readme = "README.md"` — setuptools errors out if it's missing.
#
# Trade-off: source changes invalidate the pip layer. For this
# project that's acceptable — the build is fast, and correctness
# beats a marginal cache win. (If rebuild time ever becomes a
# problem, split dependencies into requirements.txt and install
# that in a cached layer first, then `pip install --no-deps .`.)
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# Install runtime dependencies only — not the `[dev]` extras. Tests
# and linters belong in a CI image, not the production image.
RUN pip install --upgrade pip && \
    pip install .

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Create a non-root user. Running containers as root is a well-known
# security anti-pattern — a container escape, or just a bug that writes
# to the filesystem, becomes much worse when the process has root.
RUN useradd --create-home --system --uid 1000 appuser

# Copy the virtualenv from the builder. This is the entire Python
# environment: interpreter, dependencies, entry points.
COPY --from=builder /opt/venv /opt/venv

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Application source. Owned by the app user, so anything the app writes
# (which is nothing today) goes somewhere it can actually write.
WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser scripts ./scripts

USER appuser

EXPOSE 8000

# Default command. `docker-compose.yml` overrides this with `--reload`
# and bind mounts for development. A bare `docker run` uses this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]