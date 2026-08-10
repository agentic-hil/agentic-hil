"""A CAN adapter bridge that speaks protocol 2 over stdio and touches no hardware.

The broker under test opens the *real* ``adapter: process`` path — the adapter
abstraction is deliberately untouched by the broker work, so the way to keep the
broker tests honest about it is to put a real child process behind it rather than
a stub in the broker's own process.

Traffic is scripted through the environment so a test can decide what the medium
carries without a second channel:

``FAKE_CAN_BRIDGE_RX``
    JSON list of frame objects (``{"id": .., "data_hex": ..}``) handed out, in
    order, one batch per ``read``. An exhausted script reads empty, which is what
    a quiet bus does.
``FAKE_CAN_BRIDGE_FAIL_READ``
    ``read`` answers ``ok: false`` from this call number on, which is how a
    controller error is staged.
``FAKE_CAN_BRIDGE_FAIL_SEND``
    ``send`` answers ``ok: false`` with no ``side_effect_status``, the way a
    direct adapter's own ``send()`` reports a backend error whose transmit effect
    it cannot disprove. This is the returned-failure path, distinct from the
    adapter raising.
``FAKE_CAN_BRIDGE_TX_LOG``
    Path a line of JSON is appended to per accepted ``send``, so a test can see
    what actually reached the medium rather than what the broker said it sent.
"""
from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = 2


def _scripted_reads() -> list[list[dict]]:
    raw = os.environ.get("FAKE_CAN_BRIDGE_RX", "")
    if not raw:
        return []
    value = json.loads(raw)
    return [batch if isinstance(batch, list) else [batch] for batch in value]


def _normalized(frame: dict) -> dict:
    identifier = int(frame.get("id", 0))
    data_hex = str(frame.get("data_hex", ""))
    return {
        "id": identifier,
        "id_hex": f"0x{identifier:x}",
        "extended": bool(frame.get("extended", False)),
        "rtr": bool(frame.get("rtr", False)),
        "data_hex": data_hex,
        "dlc": len(bytes.fromhex(data_hex)),
    }


def main() -> int:
    reads = _scripted_reads()
    fail_read_from = int(os.environ.get("FAKE_CAN_BRIDGE_FAIL_READ", "0") or 0)
    fail_send = bool(os.environ.get("FAKE_CAN_BRIDGE_FAIL_SEND", ""))
    tx_log = os.environ.get("FAKE_CAN_BRIDGE_TX_LOG", "")
    read_calls = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        params = request.get("params") or {}
        if method == "open":
            result = {"ok": True, "protocol_version": PROTOCOL_VERSION, "backend": "fake-can-bridge", "listen_only": bool(params.get("listen_only", False))}
        elif method == "close":
            result = {"ok": True, "protocol_version": PROTOCOL_VERSION, "safe_state_confirmed": True}
        elif method == "send":
            if fail_send:
                result = {"ok": False, "error_type": "can_send_failed", "summary": "Fake CAN bridge controller send error.", "backend_error": "staged send failure"}
            else:
                if tx_log:
                    with open(tx_log, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(params.get("frame")) + "\n")
                result = {"ok": True, "backend": "fake-can-bridge"}
        elif method == "read":
            read_calls += 1
            if fail_read_from and read_calls >= fail_read_from:
                result = {"ok": False, "error_type": "can_read_failed", "summary": "Fake CAN bridge controller error."}
            else:
                batch = reads.pop(0) if reads else []
                result = {"ok": True, "backend": "fake-can-bridge", "frames": [_normalized(frame) for frame in batch]}
        else:
            result = {"ok": False, "error_type": "can_adapter_unknown_method", "summary": f"Fake CAN bridge cannot answer {method}."}
        sys.stdout.write(json.dumps({"id": request.get("id"), "result": result}) + "\n")
        sys.stdout.flush()
        if method == "close":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
