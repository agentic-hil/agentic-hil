"""Redaction of secret-named values from operator-facing output.

Defense-in-depth for the CLI, MCP, and workspace-stream serializers: any value
whose key looks like a credential (``*_token``, ``*secret``, ``password``,
``api_key``, ``authkey``) is replaced with a placeholder before it is serialized
to such a sink, so a future field carrying a real secret cannot leak in clear
text. The internal ownership marker is deliberately NOT a secret-named field and
is not a credential.

A key name cannot see a secret that a captured process stream prints inside its
value, and package managers print exactly that: pip and uv echo the index they
were configured with, and a private index URL routinely carries
``https://user:token@host/simple/``. Values under a captured stream key
(``stdout``, ``stderr``, at any depth) therefore get a second, content pass that
masks the credential shapes such output actually carries: URL userinfo, an
``Authorization: Bearer``/``Basic`` header line, and a secret-named assignment or
query parameter. The pass is deliberately surgical, and deliberately confined to
those keys: a captured stream is published so the operator reads the tool's own
words, so only the secret span is replaced and every other byte survives, and no
content pass runs over ordinary prose fields (a ``summary``, a ``likely_causes``
entry, an operator statement) where a false positive would eat the diagnosis
instead of a credential.

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

_REDACTION_MARKER = "[redacted]"

_SENSITIVE_KEY_PATTERN = re.compile(r"(?:^|_)(?:token|secret|password|passwd|apikey|api_key|authkey|auth_key)$", re.IGNORECASE)

# The keys a captured process stream arrives under, matched at any depth (the
# upgrade path nests them as `install.stdout`/`install.stderr`).
_STREAM_KEYS = frozenset({"stdout", "stderr"})

# `scheme://user:secret@host`: the user survives, because it names the account
# the manager was configured with and is not the credential, and the span
# between that colon and the `@` is masked. A URL with no password (no colon
# inside the userinfo, or an empty one) matches nothing.
_URL_USERINFO_PATTERN = re.compile(r"(?P<keep>[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@]+:)[^\s/?#@]+(?=@)")

# `Authorization: Bearer <token>` and `Authorization: Basic <blob>`, including
# the quoted spellings that appear when a manager echoes a header dict or a curl
# command. The scheme word is kept so the line still says which credential kind
# was sent; only the credential itself goes.
_AUTH_HEADER_PATTERN = re.compile(r"(?P<keep>\bauthorization[\"']?\s*:\s*[\"']?(?:bearer|basic)\s+)[^\s\"',;]+", re.IGNORECASE)

# `token=`, `password=`, `api_key=` and their relatives, as a query parameter, an
# environment assignment or a command-line flag. The name vocabulary is the key
# pattern's, and so is its rule that a prefix must be separated (`access_token`
# and `--client-secret` count, `mytoken` does not), so the two passes agree on
# what a credential is called; a hyphen joins the separator role an underscore
# has in a key, because that is how the same names are spelled on a command line
# and in a query string. Only `=` assigns: a colon is how prose introduces a
# clause, and `token: expired` is a diagnosis rather than a credential.
_ASSIGNMENT_NAME = (
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+[_\-])*"
    r"(?:token|secret|password|passwd|api[_\-]?key|auth[_\-]?key)\s*=\s*"
)

# A value's terminators depend on the context it sits in, and a query string and a
# shell command line do not share them: `)` and `;` are ordinary data inside a
# query value but end a shell word, and `#` ends a query (its fragment) but is
# ordinary data inside a shell word. Applying the union of both to every
# assignment therefore stopped a mask short of the real value end and leaked the
# tail, so the two are split into their own arms, told apart by the character
# before the name — a `?` or `&` marks a query parameter, nothing else does.

# A query parameter (`?token=…`, `&api_key=…`): the value runs to whitespace,
# another `&`, a `#` fragment, or a quote, and through the `)`, `;` and `,` that
# are ordinary inside a query value, so `?access_token=alpha)beta&next=1` masks
# `alpha)beta` rather than leaking `)beta`.
_SENSITIVE_ASSIGNMENT_QUERY = re.compile(
    r"(?P<keep>(?<=[?&])" + _ASSIGNMENT_NAME + r")[^\s\"'&#]+",
    re.IGNORECASE,
)

# A shell / environment / flag assignment: the value is a shell word, so it ends
# only at unescaped whitespace or one of `;|&<>()`, and is built from any run of
# escaped characters, single- and double-quoted segments, and ordinary characters
# — `#` and `,` among them, both data mid-word. Reading the whole word is what
# keeps an escaped space (`alpha\ beta`), an embedded `#` (`alpha#beta`) and
# concatenated quoted segments (`alpha"beta gamma"delta`) from leaking their tail
# past a mask that stopped at the first such character. The trailing arm is a value
# whose quote never closes, which the shell reads as the rest of its line, so the
# mask follows it there rather than eating the lines that follow. A value that is
# exactly one quoted segment keeps its quotes around the marker so the line still
# reads as an assignment (`PASSWORD="[redacted]"`), an unterminated one keeps its
# opening quote (`PASSWORD='[redacted]`), and everything else is masked bare; a
# second pass is a no-op because `[redacted]` is itself such a value.
_SENSITIVE_ASSIGNMENT_SHELL = re.compile(
    r"(?P<keep>" + _ASSIGNMENT_NAME + r")"
    r"(?=[^\s;|&<>()])"
    r"(?P<value>"
    r"(?:\\[\s\S]|'[^']*'|\"(?:\\[\s\S]|[^\"\\])*\"|[^\s;|&<>()\"'\\])*"
    r"(?P<unterm>'[^'\r\n]*|\"(?:\\[^\r\n]|[^\"\\\r\n])*)?"
    r")",
    re.IGNORECASE,
)

# Exactly one quoted segment and nothing else: whether a masked shell value keeps
# its surrounding quotes turns on this, so an escaped quote inside (`"a\"b"`) still
# counts as one segment.
_SINGLE_QUOTED_SEGMENT = re.compile(r"'[^']*'|\"(?:\\[\s\S]|[^\"\\])*\"")


def _mask_keep(match: re.Match[str]) -> str:
    """Keep the labelling prefix, replace the value with the marker."""
    return match.group("keep") + _REDACTION_MARKER


def _mask_shell(match: re.Match[str]) -> str:
    """Keep the assignment prefix and mask the value, keeping the quotes a wholly
    quoted value carries and the opening quote an unterminated one carries so the
    masked line still reads as an assignment."""
    keep = match.group("keep")
    value = match.group("value")
    if value.startswith(("'", '"')):
        if _SINGLE_QUOTED_SEGMENT.fullmatch(value):
            return keep + value[0] + _REDACTION_MARKER + value[0]
        if match.group("unterm"):
            return keep + value[0] + _REDACTION_MARKER
    return keep + _REDACTION_MARKER


# Each pass is paired with how its match is rebuilt. The query pass runs before the
# shell pass so a query value is masked with the query's own terminators; the shell
# pass then finds nothing left to do on it (the `[redacted]` it wrote is itself a
# valid shell value that re-masks to `[redacted]`).
_CONTENT_PATTERNS = (
    (_URL_USERINFO_PATTERN, _mask_keep),
    (_AUTH_HEADER_PATTERN, _mask_keep),
    (_SENSITIVE_ASSIGNMENT_QUERY, _mask_keep),
    (_SENSITIVE_ASSIGNMENT_SHELL, _mask_shell),
)


def redact_stream_text(text: str) -> str:
    """Mask the credential-shaped spans inside one captured process stream.

    Everything outside a masked span is returned byte for byte, because the
    stream is the tool's own account of what went wrong.
    """
    for pattern, replace in _CONTENT_PATTERNS:
        text = pattern.sub(replace, text)
    return text


def redact_sensitive(value: object) -> object:
    """Recursively replace values under secret-named keys with a placeholder and
    mask credential-shaped spans inside captured stream values."""
    if isinstance(value, dict):
        return {key: _redact_member(key, item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _redact_member(key: object, value: object) -> object:
    """One mapping member: the key name decides which of the two passes it takes."""
    if isinstance(key, str):
        if _SENSITIVE_KEY_PATTERN.search(key):
            return _REDACTION_MARKER
        if key.lower() in _STREAM_KEYS:
            return _redact_stream_member(value)
    return redact_sensitive(value)


def _redact_stream_member(value: object) -> object:
    """Mask stream text, whether it arrives as one blob or as a list of lines.
    Anything else under the key is not a stream and takes the ordinary pass."""
    if isinstance(value, str):
        return redact_stream_text(value)
    if isinstance(value, list):
        return [_redact_stream_member(item) for item in value]
    return redact_sensitive(value)


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
