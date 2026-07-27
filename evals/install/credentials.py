from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# A token that expires during the run makes the agent CLI refresh inside the
# container. The provider may rotate the refresh token when it does, and the
# rotated value dies with the container while this machine keeps the old one.
# Refreshing on the host beforehand avoids that entirely.
REFRESH_MARGIN = timedelta(hours=1)

# An agent CLI reports an unusable login before it does anything else, so these
# phrases separate a dead credential from a failure the evaluation is about.
AUTHENTICATION_FAILURES = (
    re.compile(r"token refresh failed", re.IGNORECASE),
    re.compile(r"\b(401|403)\b.{0,40}\b(unauthorized|forbidden)\b", re.IGNORECASE),
    re.compile(r"\b(unauthorized|invalid[_ ]api[_ ]key|authentication[_ ]error)\b", re.IGNORECASE),
    re.compile(r"\bnot logged in\b|\bplease (run )?(re-?)?login\b", re.IGNORECASE),
    re.compile(r"\bOAuth token (has )?expired\b", re.IGNORECASE),
)


def authentication_failure(text: str) -> str | None:
    """The phrase that shows the agent CLI could not authenticate at all."""
    for pattern in AUTHENTICATION_FAILURES:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return None


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    # Both agent CLIs store milliseconds since the epoch.
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def credential_health(kind: str, path: Path, now: datetime | None = None) -> tuple[str, str]:
    """Report whether a stored login can still be refreshed.

    Returns one of ``ok``, ``stale`` (the access token expired but a refresh
    token is stored), ``expired`` (nothing left to refresh with), or ``unknown``
    when the file states no expiry at all.
    """
    moment = now or datetime.now(timezone.utc)
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return "expired", f"{path} cannot be read: {type(error).__name__}"
    if not isinstance(document, dict):
        return "unknown", f"{path} is not an object"

    if kind == "claude-auth":
        section = document.get("claudeAiOauth")
        if not isinstance(section, dict):
            return "unknown", f"{path} carries no claudeAiOauth section"
        refresh_expiry = _moment(section.get("refreshTokenExpiresAt"))
        if refresh_expiry is not None and refresh_expiry <= moment:
            return "expired", f"the refresh token expired at {refresh_expiry.isoformat()}"
        access_expiry = _moment(section.get("expiresAt"))
        if access_expiry is not None and access_expiry <= moment + REFRESH_MARGIN:
            return "stale", f"the access token expires at {access_expiry.isoformat()}; a refresh is due"
        return "ok", f"valid until {access_expiry.isoformat()}" if access_expiry else "no expiry stated"

    if kind == "opencode-auth":
        providers = {name: value for name, value in document.items() if isinstance(value, dict)}
        if not providers:
            return "unknown", f"{path} lists no provider"
        usable = []
        for name, provider in providers.items():
            expiry = _moment(provider.get("expires"))
            if expiry is None or expiry > moment + REFRESH_MARGIN:
                usable.append(f"{name} valid")
            elif provider.get("refresh"):
                usable.append(f"{name} stale, refresh due")
            else:
                usable.append(f"{name} expired without a refresh token")
        if all("expired without" in entry for entry in usable):
            return "expired", "; ".join(usable)
        if any("stale" in entry for entry in usable):
            return "stale", "; ".join(usable)
        return "ok", "; ".join(usable)

    return "unknown", f"{path} states no expiry"
