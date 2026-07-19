"""Redaction of secret-named values from operator-facing output.

Coordination records carry an internal ``owner_token`` nonce (a per-process
``secrets.token_hex`` used only to match process ownership). It is not an
external credential, but it must never be emitted in clear text to an operator
terminal, an MCP client, or a workspace stream. This module strips any
secret-named key from a value before it is serialized to such a sink.
"""
from __future__ import annotations

import re

_SENSITIVE_KEY_PATTERN = re.compile(r"(?:^|_)(?:token|secret|password|passwd|apikey|api_key)$", re.IGNORECASE)


def redact_sensitive(value: object) -> object:
    """Recursively replace values under secret-named keys with a placeholder."""
    if isinstance(value, dict):
        return {key: ("[redacted]" if isinstance(key, str) and _SENSITIVE_KEY_PATTERN.search(key) else redact_sensitive(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value
