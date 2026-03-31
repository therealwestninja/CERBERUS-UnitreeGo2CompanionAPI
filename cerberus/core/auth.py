"""
cerberus/core/auth.py
━━━━━━━━━━━━━━━━━━━━
API key authentication for the CERBERUS backend.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

_SIMULATION: bool = os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1")
_API_KEY: str | None = os.getenv("CERBERUS_API_KEY", "").strip() or None

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


async def require_api_key(request: Request) -> None:
    if _API_KEY is None:
        return

    if request.url.path in (
        "/health",
        "/ready",
        "/api/v1/system/health",
        "/api/v1/system/ready",
    ):
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

    if not secrets.compare_digest(provided.encode(), _API_KEY.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey realm=\"CERBERUS\""},
        )


def auth_enabled() -> bool:
    return _API_KEY is not None
