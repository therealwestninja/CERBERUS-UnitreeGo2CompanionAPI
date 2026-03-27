# go2_platform/Dockerfile
# ══════════════════════════════════════════════════════════════════════════
# Multi-stage build: slim Python 3.11 base, no dev dependencies in prod.
# ══════════════════════════════════════════════════════════════════════════

# ── Stage 1: builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt pyproject.toml ./
COPY backend/ ./backend/

# Install build dependencies
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e .

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Create non-root user for safety
RUN groupadd -r go2 && useradd -r -g go2 -d /app -s /sbin/nologin go2

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=go2:go2 backend/    ./backend/
COPY --chown=go2:go2 ui/         ./ui/
COPY --chown=go2:go2 plugins/    ./plugins/
COPY --chown=go2:go2 config/     ./config/
COPY --chown=go2:go2 .env.example ./.env.example

# Create runtime directories
RUN mkdir -p /tmp/go2_ota_backups && chown go2:go2 /tmp/go2_ota_backups

USER go2

# Environment defaults (override via docker run -e or --env-file)
ENV GO2_HOST=0.0.0.0 \
    GO2_PORT=8080 \
    GO2_MODE=simulation \
    GO2_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "backend.api.server:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--log-level", "info"]
