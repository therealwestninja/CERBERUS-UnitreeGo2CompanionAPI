# CERBERUS v3.2 — Dockerfile
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt pyproject.toml ./
COPY cerberus/ ./cerberus/
COPY backend/  ./backend/
COPY plugins/  ./plugins/
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e .

FROM python:3.11-slim AS runtime
RUN groupadd -r cerberus && useradd -r -g cerberus -d /app cerberus
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin            /usr/local/bin
COPY --chown=cerberus:cerberus cerberus/ ./cerberus/
COPY --chown=cerberus:cerberus backend/  ./backend/
COPY --chown=cerberus:cerberus plugins/  ./plugins/
COPY --chown=cerberus:cerberus ui/       ./ui/
COPY --chown=cerberus:cerberus config/   ./config/
COPY --chown=cerberus:cerberus main.py   ./main.py
COPY --chown=cerberus:cerberus .env.example ./.env.example

RUN mkdir -p /app/logs /app/funscripts && chown -R cerberus:cerberus /app/logs /app/funscripts
USER cerberus

ENV GO2_SIMULATION=false \
    GO2_API_HOST=0.0.0.0 \
    GO2_API_PORT=8080 \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Use create_app factory — restores original Dockerfile CMD
CMD ["uvicorn", "backend.api.server:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--log-level", "info"]
