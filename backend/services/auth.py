import logging
import os
import secrets

from fastapi import Header, HTTPException

logger = logging.getLogger("voice_agent.auth")

ACCESS_KEY = os.getenv("DASHBOARD_ACCESS_KEY")
if not ACCESS_KEY:
    ACCESS_KEY = secrets.token_urlsafe(16)
    logger.warning("DASHBOARD_ACCESS_KEY not set — generated a temporary key for this session:")
    logger.warning("  %s", ACCESS_KEY)
    logger.warning("Set DASHBOARD_ACCESS_KEY in .env to keep a stable key across restarts.")


def verify_key(x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key")):
    if not x_dashboard_key or not secrets.compare_digest(x_dashboard_key, ACCESS_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing dashboard key")
