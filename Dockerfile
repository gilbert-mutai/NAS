# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build tools live only in this stage; they are not present in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy metadata first so the dependency layer is cached independently of source
# changes — editing a module does not trigger a full reinstall.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel \
    && pip install .


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Runs as an unprivileged user. NAS holds the credentials that reach production
# switches, so the process gets no more privilege than it needs.
RUN groupadd --gid 1001 nas \
    && useradd --uid 1001 --gid nas --create-home --shell /usr/sbin/nologin nas

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY src ./src

USER nas

EXPOSE 8000

# Binds all interfaces because the container's network namespace is the boundary;
# restrict exposure at the compose/Nginx layer, not here.
CMD ["uvicorn", "nas.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
