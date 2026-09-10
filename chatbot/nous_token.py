"""Nous Portal token reader for the SisengAI bot services.

The bot uses the same Nous Portal subscription as Hermes (paid, no free-tier
hourly caps). Tokens live in ~/.hermes/auth.json under providers.nous.

IMPORTANT: this module only READS tokens — it never calls the refresh
endpoint. Nous refresh tokens are single-use, and the Portal revokes the whole
session if anything other than Hermes redeems one (refresh_token_reused).
Hermes itself rotates access_token/agent_key whenever it runs; we just pick up
the freshest one. If the token is missing/expired, callers keep their existing
fallback chain (OpenRouter free models) instead of refreshing.
"""

import json
import os
import time
from pathlib import Path


def _auth_path() -> Path:
    home = os.environ.get("HERMES_HOME", str(Path.home()))
    return Path(home) / ".hermes" / "auth.json"


def get_nous_token() -> str:
    """Return a fresh Nous bearer token, or '' if unavailable/expired."""
    try:
        data = json.loads(_auth_path().read_text())
        state = data.get("providers", {}).get("nous", {})
    except Exception:
        return ""
    # Prefer the agent key (what Hermes uses for agent traffic), else access token.
    for key in ("agent_key", "access_token"):
        token = state.get(key)
        exp = state.get(key.replace("agent_key", "agent_key_expires_at")
                        if key == "agent_key" else "expires_at")
        if not token:
            continue
        if exp:
            try:
                if time.time() > _parse_expiry(exp) - 120:
                    continue  # expiring within 2 min: let the fallback handle it
            except Exception:
                pass
        return token
    return ""


def _parse_expiry(value) -> float:
    """Parse an ISO-8601 timestamp (with Z or +00:00 offset) to epoch seconds."""
    from datetime import datetime, timezone
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
