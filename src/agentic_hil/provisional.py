"""Process-wide registry of provisionally-owned raw hardware handles.

A raw handle (serial port, python-can Bus, adapter bridge) is opened before the
session object that will own it exists. If the session constructor fails and the
immediate rollback close also fails, the raw handle would otherwise become
unreachable — still live on the hardware, but with no reference for a second
cleanup attempt. Registering the handle here the moment it is opened keeps it
reachable so a later ``service.close()`` can retry the close until it succeeds.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import NamedTuple

_GUARD = threading.Lock()
_HANDLES: dict[int, _ProvisionalHandle] = {}
_COUNTER = 0


class _ProvisionalHandle(NamedTuple):
    owner_marker: str | None
    label: str
    close: Callable[[], object]


def register_provisional_handle(owner_marker: str | None, label: str, close: Callable[[], object]) -> int:
    """Register a raw handle's closer and return a token used to discharge it
    once a session has taken durable ownership."""
    global _COUNTER
    with _GUARD:
        _COUNTER += 1
        token = _COUNTER
        _HANDLES[token] = _ProvisionalHandle(owner_marker, label, close)
    return token


def discharge_provisional_handle(token: int) -> None:
    """Drop a provisional registration after ownership has been transferred to a
    published session (or the handle has been confirmed closed)."""
    with _GUARD:
        _HANDLES.pop(token, None)


def cleanup_provisional_handles(owner_marker: str) -> list[str]:
    """Close every still-registered provisional handle for an owner. Entries
    whose close succeeds are removed; entries that still fail stay registered so
    a later call retries them. Returns one error string per failed handle."""
    errors: list[str] = []
    interrupt: KeyboardInterrupt | SystemExit | None = None
    with _GUARD:
        targets = [(token, entry) for token, entry in _HANDLES.items() if entry.owner_marker == owner_marker]
    for token, entry in targets:
        try:
            entry.close()
        except BaseException as error:  # noqa: BLE001 - best-effort cleanup, aggregated
            errors.append(f"{entry.label}: {type(error).__name__}: {error}")
            if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                interrupt = error
            continue
        with _GUARD:
            _HANDLES.pop(token, None)
    if interrupt is not None:
        # Every handle was still attempted; only now, after the best-effort
        # sweep, is the interrupt propagated instead of masked as an error string.
        raise interrupt
    return errors


def provisional_handle_count(owner_marker: str | None = None) -> int:
    """Number of outstanding provisional handles (optionally for one owner).
    Test/diagnostic helper."""
    with _GUARD:
        if owner_marker is None:
            return len(_HANDLES)
        return sum(1 for entry in _HANDLES.values() if entry.owner_marker == owner_marker)
