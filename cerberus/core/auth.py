"""
cerberus/core/auth.py
━━━━━━━━━━━━━━━━━━━━
API key authentication for the CERBERUS backend.

Security model:
  • All REST endpoints require the  X-CERBERUS-Key  header.
  • WebSocket /ws accepts the key as a ?api_key=  query parameter
    (browsers cannot set custom headers on WS upgrades).
  • Keys are compared with secrets.compare_digest — safe against timing attacks.
  • In SIMULATION mode, the key is optional (dev-friendly). A warning is logged.
  • In REAL-HARDWARE mode, CERBERUS_API_KEY MUST be set — startup fails otherwise.

Usage:
  # Generate a key once, store in .env:
  python -c "import secrets; print(secrets.token_hex(32))"

  # .env:
  CERBERUS_API_KEY=<your 64-char hex key>

Integration (main.py):
  from cerberus.core.auth import require_api_key
  app = FastAPI(..., dependencies=[Depends(require_api_key)])

  # For WS: the WS endpoint also receives this dependency automatically.
  # WS clients should connect as:  ws://host/ws?api_key=<key>
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_SIMULATION: bool = os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1")
_API_KEY: str | None = os.getenv("CERBERUS_API_KEY", "").strip() or None

# Fail fast if real hardware and no key is set.
if not _SIMULATION and not _API_KEY:
    raise RuntimeError(
        "CERBERUS_API_KEY must be set when GO2_SIMULATION is not true.\n"
        "Generate a key:  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "Then add it to your .env file as CERBERUS_API_KEY=<key>"
    )

if not _API_KEY:
    logger.warning(
        "⚠️  CERBERUS_API_KEY is not set — authentication disabled. "
        "Set CERBERUS_API_KEY before deploying on real hardware."
    )
else:
    logger.info("✅  API key authentication enabled (%d chars)", len(_API_KEY))


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def require_api_key(request: Request) -> None:
    """
    FastAPI dependency.  Applied as a global app dependency so every
    endpoint — including WebSocket routes — is protected automatically.

    Key lookup order (first match wins):
      1. X-CERBERUS-Key  request header  (REST clients)
      2. api_key         query parameter  (WebSocket clients, curl tests)
    """
    if _API_KEY is None:
        # Simulation mode, no key configured → allow everything
        return

    # Liveness/readiness probes must be reachable by orchestrators without a key
    if request.url.path in ("/health", "/ready"):
        return

    provided: str | None = (
        request.headers.get("X-CERBERUS-Key")
        or request.query_params.get("api_key")
    )

    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Send X-CERBERUS-Key header or ?api_key= query param.",
            headers={"WWW-Authenticate": "ApiKey realm=\"CERBERUS\""},
        )

    # Constant-time comparison — prevents timing-oracle key enumeration
    if not secrets.compare_digest(provided.encode(), _API_KEY.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey realm=\"CERBERUS\""},
        )


# ── Utility ───────────────────────────────────────────────────────────────────

def auth_enabled() -> bool:
    """Returns True if an API key is configured (for /health responses)."""
    return _API_KEY is not None
