"""Redaction of secret-named values from operator-facing output.

Defense-in-depth for the CLI, MCP, and workspace-stream serializers: any value
whose key looks like a credential (``*_token``, ``*secret``, ``password``,
``api_key``, ``authkey``) is replaced with a placeholder before it is serialized
to such a sink, so a future field carrying a real secret cannot leak in clear
text. The internal ownership marker is deliberately NOT a secret-named field and
is not a credential.

``authkey`` is here because of the CAN broker's HMAC handshake key, and it is the
second of two measures rather than the only one: the broker and its participants
keep the key out of every result payload by construction, and this pattern is
what catches the field somebody adds later without reading that argument. Note
that the pattern is anchored at the end of the key and ``authkey`` does not end
in ``key``-as-a-word — ``token|secret|...`` never matched it — so the arm is
explicit rather than a widening of the existing one, which would also have
started redacting ``resource_key``-shaped fields that are not credentials.
"""
from __future__ import annotations

import re

_SENSITIVE_KEY_PATTERN = re.compile(r"(?:^|_)(?:token|secret|password|passwd|apikey|api_key|authkey|auth_key)$", re.IGNORECASE)


def redact_sensitive(value: object) -> object:
    """Recursively replace values under secret-named keys with a placeholder."""
    if isinstance(value, dict):
        return {key: ("[redacted]" if isinstance(key, str) and _SENSITIVE_KEY_PATTERN.search(key) else redact_sensitive(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def filesystem_error_detail(error: BaseException) -> dict:
    """Describe a filesystem error WITHOUT its absolute filename. ``str(OSError)``
    embeds the path (``[Errno 13] Permission denied: 'C:\\...\\state-root\\...'``),
    which would leak an environment-derived state-root/config path to an operator
    or MCP sink; emit only the error class and errno instead."""
    # Use "error_class", never "error_type": callers spread this into a result
    # that already carries a classifying "error_type" which must not be clobbered.
    detail: dict = {"error_class": getattr(error, "error_type", None) or type(error).__name__}
    errno = getattr(error, "errno", None)
    if isinstance(errno, int):
        detail["errno"] = errno
    return detail
