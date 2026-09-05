from __future__ import annotations

import base64
import contextlib
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

# What a provider says when the refresh token in a stored login has already been
# spent. A refresh token is single use: the copy that refreshes first receives
# the next one, and every other copy of that login is left holding a value the
# provider has already replaced. No file states this, because the file cannot
# know what another copy of it did, so the only way to learn it is to try the
# refresh and read the answer.
#
# Only wording that names reuse belongs here. A spent token is permanent -- the
# only cure is to sign in again -- so this must not match a phrase a transient
# failure can also print. `failed to refresh token` is exactly that: it prefixes
# `failed to refresh token: connection timed out` as readily as a reuse, so it
# lives in AUTHENTICATION_FAILURES below and not here, where it would report a
# retriable outage as a credential the operator has to replace.
SPENT_REFRESH_TOKEN = re.compile(
    r"refresh token was already used|please log out and sign in again",
    re.IGNORECASE,
)

# An agent CLI reports an unusable login before it does anything else, so these
# phrases separate a dead credential from a failure the evaluation is about. The
# generic refresh failures -- either word order -- sit here rather than in
# SPENT_REFRESH_TOKEN: they show authentication did not happen without claiming
# the token is spent, which is the narrower thing only the reuse wording proves.
AUTHENTICATION_FAILURES = (
    re.compile(r"token refresh failed", re.IGNORECASE),
    re.compile(r"failed to refresh token", re.IGNORECASE),
    SPENT_REFRESH_TOKEN,
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


def spent_refresh_token(text: str) -> str | None:
    """The CLI's own line saying the stored refresh token has already been used.

    The whole line rather than the phrase inside it, because this is the one
    credential answer nothing on this machine can derive for itself: whoever
    reads it has to be able to see what the CLI actually said, including the
    provider's own wording for what to do about it.
    """
    for line in text.splitlines():
        if SPENT_REFRESH_TOKEN.search(line):
            return line.strip()
    return None


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    # Both agent CLIs store milliseconds since the epoch.
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _claimed_expiry(token: Any) -> datetime | None:
    """When a stored JSON Web Token says it stops being accepted.

    codex keeps no expiry field of its own: its `auth.json` states when the
    login was last refreshed, and the moment that matters is inside the access
    token, whose middle segment is a base64url-encoded claim set carrying `exp`
    in seconds since the epoch rather than the milliseconds the other two CLIs
    write into a field.

    Anything that does not decode into that shape is not this function's to
    interpret. It answers None, and the caller reports a login whose expiry it
    could not read rather than inventing one.
    """
    if not isinstance(token, str):
        return None
    segments = token.split(".")
    if len(segments) != 3:
        return None
    payload = segments[1]
    try:
        # base64url without the padding the encoder is allowed to omit.
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        # binascii.Error and UnicodeDecodeError are both ValueError, so a
        # segment that is not base64, not UTF-8 or not JSON lands here.
        return None
    if not isinstance(claims, dict):
        return None
    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
        return None
    return datetime.fromtimestamp(expiry, timezone.utc)


def stored_expiry(kind: str, path: Path) -> str:
    """What the file itself says about when its access token stops being accepted.

    The moment where the file states one, and a sentence naming why there is none
    where it does not. Read straight off the file rather than out of a health
    verdict, because the question this answers is whether the file moved after a
    CLI ran, and two verdicts can read the same while the documents under them do
    not. Never raises: a caller reporting evidence about a refusal must not fail
    while assembling the evidence.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return f"unreadable ({type(error).__name__})"
    if not isinstance(document, dict):
        return "no expiry stated"
    moment: datetime | None = None
    if kind == "claude-auth":
        section = document.get("claudeAiOauth")
        if isinstance(section, dict):
            moment = _moment(section.get("expiresAt"))
    elif kind == "codex-auth":
        tokens = document.get("tokens")
        if isinstance(tokens, dict):
            moment = _claimed_expiry(tokens.get("access_token"))
    return moment.isoformat() if moment is not None else "no expiry stated"


# Short enough that a field name or a scope survives into a log where it helps,
# long enough that no token slips under it.
SECRET_LENGTH = 8


def _secret_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if len(value) >= SECRET_LENGTH else []
    if isinstance(value, list):
        return [secret for item in value for secret in _secret_strings(item)]
    if isinstance(value, dict):
        return [secret for item in value.values() for secret in _secret_strings(item)]
    return []


def stored_secrets(path: Path) -> list[str]:
    """Every literal out of a stored login that must never reach a log or a terminal.

    The path, the document whole, and every string inside it long enough to be a
    token. This is the set the evaluation hands its redactor for captured agent
    output, and anything printing a CLI's own output about a login owes its
    reader the same pass: a refresh failure is exactly the moment a CLI is
    likeliest to quote the credential back.

    Never raises, for the same reason `stored_expiry` does not.
    """
    resolved = Path(path)
    values = [str(resolved), resolved.as_posix()]
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [value for value in dict.fromkeys(values) if value]
    values.append(content)
    with contextlib.suppress(json.JSONDecodeError):
        values.extend(_secret_strings(json.loads(content)))
    return [value for value in dict.fromkeys(values) if value]


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

    if kind == "codex-auth":
        tokens = document.get("tokens")
        if not isinstance(tokens, dict):
            api_key = document.get("OPENAI_API_KEY")
            if isinstance(api_key, str) and api_key.strip():
                # A key rather than a session: it carries no refresh token, so
                # there is nothing here a container could spend.
                return "ok", "an API key is stored, and an API key has nothing to refresh"
            return "expired", f"{path} carries neither a tokens section nor an API key"
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return "expired", "the stored login carries no refresh token"
        access_expiry = _claimed_expiry(tokens.get("access_token"))
        if access_expiry is None:
            # Reported rather than papered over: without an expiry this cannot
            # say whether the container will refresh, and a guess here is what
            # decides whether a refresh happens on this machine or in a
            # container that takes the rotated token with it.
            return "unknown", f"{path} states no access token expiry"
        if access_expiry <= moment + REFRESH_MARGIN:
            return "stale", f"the access token expires at {access_expiry.isoformat()}; a refresh is due"
        return "ok", f"valid until {access_expiry.isoformat()}"

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
