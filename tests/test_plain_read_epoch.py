from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.comports import ComPortService, ComPortSession
from agentic_hil.config import load_config

PORT_ID = "plain_read_epoch_uart"
COM_PORTS_YAML = f'com_ports:\n  {PORT_ID}:\n    device: "/dev/ttyPLAINREADEPOCH"\n'


def service_with_session(tmp_path: Path, buffered: bytes) -> tuple[ComPortService, ComPortSession]:
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORTS_YAML)))
    service = ComPortService(config)
    session = ComPortSession(
        PORT_ID,
        config.com_ports[PORT_ID],
        SimpleNamespace(is_open=True),
        str(tmp_path / "session.jsonl"),
        start_reader=False,
    )
    session.buffer.extend(buffered)
    service.sessions[PORT_ID] = session
    return service, session


@pytest.mark.parametrize("read_path", [pytest.param("read_bytes", id="read_bytes"), pytest.param("_read_until", id="_read_until")])
def test_consuming_plain_read_invalidates_epoch(tmp_path: Path, read_path: str) -> None:
    buffered = b"plain feedback" if read_path == "read_bytes" else b"PASS trailing feedback"
    service, session = service_with_session(tmp_path, buffered)
    with session.lock:
        before = session.buffer_epoch

    if read_path == "read_bytes":
        result = service.read_bytes(PORT_ID, max_bytes=len(buffered), wait_timeout_s=0.0)
    else:
        result = service.read_bytes(PORT_ID, max_bytes=len(buffered), wait_timeout_s=0.0, until="PASS")

    assert result["ok"] is True, result
    assert result["bytes_read"] > 0, result
    with session.lock:
        after = session.buffer_epoch
    if after == before:
        raise AssertionError(f"{read_path} consumed a nonempty prefix without advancing buffer_epoch")


@pytest.mark.parametrize("read_path", [pytest.param("read_bytes", id="read_bytes"), pytest.param("_read_until", id="_read_until")])
def test_empty_plain_read_preserves_epoch(tmp_path: Path, read_path: str) -> None:
    service, session = service_with_session(tmp_path, b"")
    with session.lock:
        before = session.buffer_epoch

    if read_path == "read_bytes":
        result = service.read_bytes(PORT_ID, wait_timeout_s=0.0)
    else:
        result = service.read_bytes(PORT_ID, wait_timeout_s=0.0, until="PASS")

    assert result["ok"] is True, result
    with session.lock:
        assert session.buffer_epoch == before
