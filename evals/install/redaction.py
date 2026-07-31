from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, TextIO
from urllib.parse import quote, quote_plus

REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "\n[OUTPUT TRUNCATED: log content limit reached]\n"
DEFAULT_LOG_CONTENT_BYTES = 8 * 1024 * 1024

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_JSON_UNICODE_ESCAPE = re.compile(r"\\u[0-9A-Fa-f]{4}")


def _escape_case_variants(value: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    lower = pattern.sub(lambda match: match.group(0).lower(), value)
    upper = pattern.sub(lambda match: match.group(0).upper(), value)
    return tuple(dict.fromkeys((value, lower, upper)))


def _canonical_escapes(value: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub(lambda match: match.group(0).upper(), value)


def _encoded_secret_variants(secrets: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    json_variants: set[str] = set()
    url_variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        for ensure_ascii in (True, False):
            escaped = json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1]
            json_variants.update((escaped, escaped.replace("/", r"\/")))
        url_variants.update(
            (
                quote(secret),
                quote(secret, safe=""),
                quote_plus(secret, safe=""),
            )
        )
    return (
        tuple(sorted(json_variants, key=lambda value: (-len(value), value))),
        tuple(sorted(url_variants, key=lambda value: (-len(value), value))),
    )


def secret_variants(secrets: Iterable[str]) -> tuple[str, ...]:
    """Return literal forms commonly produced when a secret is logged."""
    secret_values = tuple(secrets)
    variants: set[str] = set()
    for secret in secret_values:
        if not secret:
            continue
        variants.add(secret)

    json_variants, url_variants = _encoded_secret_variants(secret_values)
    for candidate in json_variants:
        variants.update(_escape_case_variants(candidate, _JSON_UNICODE_ESCAPE))
    for candidate in url_variants:
        variants.update(_escape_case_variants(candidate, _PERCENT_ESCAPE))

    return tuple(sorted(variants, key=lambda value: (-len(value), value)))


class SecretRedactor:
    """Streaming literal redactor that preserves matches split across chunks."""

    def __init__(self, secrets: Iterable[str], marker: str = REDACTION_MARKER) -> None:
        secret_values = tuple(secrets)
        self._variants = secret_variants(secret_values)
        json_variants, url_variants = _encoded_secret_variants(secret_values)
        self._canonical_json_variants = tuple(
            _canonical_escapes(value, _JSON_UNICODE_ESCAPE) for value in json_variants
        )
        self._canonical_url_variants = tuple(_canonical_escapes(value, _PERCENT_ESCAPE) for value in url_variants)
        self._marker = marker
        self._buffer = ""
        self._max_variant_length = max((len(value) for value in self._variants), default=0)

    def _earliest_match(self) -> tuple[int, int] | None:
        selected: tuple[int, int] | None = None
        candidates = (
            (self._buffer, self._variants),
            (
                _canonical_escapes(self._buffer, _JSON_UNICODE_ESCAPE),
                self._canonical_json_variants,
            ),
            (
                _canonical_escapes(self._buffer, _PERCENT_ESCAPE),
                self._canonical_url_variants,
            ),
        )
        for searchable, variants in candidates:
            for variant in variants:
                index = searchable.find(variant)
                if index < 0:
                    continue
                match = (index, len(variant))
                if selected is None or match[0] < selected[0] or (match[0] == selected[0] and match[1] > selected[1]):
                    selected = match
        return selected

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self._variants:
            return text

        self._buffer += text
        output: list[str] = []
        while match := self._earliest_match():
            index, match_length = match
            output.extend((self._buffer[:index], self._marker))
            self._buffer = self._buffer[index + match_length :]

        if final:
            output.append(self._buffer)
            self._buffer = ""
        else:
            retained = self._max_variant_length - 1
            safe_length = max(0, len(self._buffer) - retained)
            output.append(self._buffer[:safe_length])
            self._buffer = self._buffer[safe_length:]
        return "".join(output)

    def finish(self) -> str:
        return self.feed("", final=True)

    def redact(self, text: str) -> str:
        if self._buffer:
            raise RuntimeError("cannot use one-shot redaction while streaming data is buffered")
        return self.feed(text, final=True)


def redact(text: str, secrets: Iterable[str]) -> str:
    return SecretRedactor(secrets).redact(text)


def redact_value(value: Any, secrets: Iterable[str]) -> Any:
    """Recursively redact strings, including mapping keys, before serialization."""
    variants = tuple(secrets)
    redactor = SecretRedactor(variants)

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            return redactor.redact(item)
        if isinstance(item, Mapping):
            return {visit(key): visit(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [visit(nested) for nested in item]
        if isinstance(item, tuple):
            return tuple(visit(nested) for nested in item)
        return item

    return visit(value)


def _utf8_prefix(text: str, byte_limit: int) -> tuple[str, int]:
    if byte_limit <= 0:
        return "", 0
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text, len(encoded)
    prefix = encoded[:byte_limit].decode("utf-8", errors="ignore")
    return prefix, len(prefix.encode("utf-8"))


class RedactingLogWriter:
    """Redact a stream and retain bounded log content while still draining input."""

    def __init__(
        self,
        handle: TextIO,
        secrets: Iterable[str],
        *,
        content_byte_limit: int = DEFAULT_LOG_CONTENT_BYTES,
    ) -> None:
        if content_byte_limit < 0:
            raise ValueError("content_byte_limit must not be negative")
        self._handle = handle
        self._redactor = SecretRedactor(secrets)
        self._content_byte_limit = content_byte_limit
        self._content_bytes_written = 0
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _write(self, text: str) -> None:
        if not text or self._truncated:
            return
        remaining = self._content_byte_limit - self._content_bytes_written
        prefix, byte_count = _utf8_prefix(text, remaining)
        if prefix:
            self._handle.write(prefix)
            self._content_bytes_written += byte_count
        if prefix != text:
            self._truncated = True
            self._handle.write(TRUNCATION_MARKER)

    def feed(self, text: str) -> None:
        self._write(self._redactor.feed(text))

    def finish(self) -> None:
        self._write(self._redactor.finish())
        self._handle.flush()
