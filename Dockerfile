# CERBERUS — Dockerfile  (v3.1.0)
# ════════════════════════════════════════════════════════════════════════
# Multi-stage build: slim Python 3.11 runtime with no dev tools in prod.
# ════════════════════════════════════════════════════════════════════════

# ── Stage 1: builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt pyproject.toml ./
COPY cerberus/ ./cerberus/
COPY backend/  ./backend/
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e .

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN groupadd -r cerberus && useradd -r -g cerberus -d /app -s /sbin/nologin cerberus

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin            /usr/local/bin

# Copy application code
COPY --chown=cerberus:cerberus cerberus/  ./cerberus/
COPY --chown=cerberus:cerberus backend/   ./backend/
COPY --chown=cerberus:cerberus ui/        ./ui/
COPY --chown=cerberus:cerberus plugins/   ./plugins/
COPY --chown=cerberus:cerberus config/    ./config/
COPY --chown=cerberus:cerberus .env.example ./.env.example

# Create writable runtime dirs
RUN mkdir -p /app/logs /app/data \
 && chown -R cerberus:cerberus /app/logs /app/data

USER cerberus

# Environment defaults (override via -e or --env-file)
ENV CERBERUS_HOST=0.0.0.0 \
    CERBERUS_PORT=8080 \
    CERBERUS_CONFIG=config/cerberus.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "backend.api.server:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--log-level", "info"]
