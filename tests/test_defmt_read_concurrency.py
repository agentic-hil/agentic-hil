"""Concurrent defmt reads must commit each buffered frame exactly once."""

from __future__ import annotations

import threading

import pytest
from defmt_wire import encode_frame
from support import scaled_time_bound

from agentic_hil import comports
from agentic_hil.comports import ComPortSession, DefmtStream
from agentic_hil.defmt import DefmtBinding, DefmtTable
from agentic_hil.types import ComPortConfig

DECODE_TIMEOUT_S = scaled_time_bound(5)
EXPECTED_FAILURE = "concurrent defmt reads must deliver value 1 and value 2 exactly once each"


def test_concurrent_defmt_reads_consume_each_frame_once(monkeypatch: pytest.MonkeyPatch) -> None:
    table = DefmtTable({1: ("defmt_info", "value {=u8}")}, None, "raw", "4")
    frames = [encode_frame(table, "value {=u8}", [value]) for value in (1, 2)]
    frame_size = len(frames[0])
    session = ComPortSession(
        "dut",
        ComPortConfig(
            device="in-memory",
            baudrate=115200,
            timeout_s=0.1,
            write_timeout_s=0.1,
            encoding="utf-8",
            max_buffer_bytes=sum(map(len, frames)) + 1,
            max_write_bytes=64,
        ),
        object(),
        "unused.log",
        start_reader=False,
    )
    session.defmt = DefmtStream(DefmtBinding(table, {"proven": False}), None)
    session.buffer.extend(b"".join(frames))
    service = object.__new__(comports.ComPortService)
    preview, _, _, _ = service._take_frames(session, frame_size, [], commit=False)
    assert [record["message"] for record in preview] == ["value 1"]

    first_decodes: set[int] = set()
    first_decode_lock = threading.Lock()
    first_decode_barrier = threading.Barrier(2)
    original_decode = comports._decode_head

    def synchronize_first_decode(table, head, entries, desynchronized, full):
        records = original_decode(table, head, entries, desynchronized, full)
        reader_id = threading.get_ident()
        with first_decode_lock:
            first_decode = reader_id not in first_decodes
            first_decodes.add(reader_id)
        if first_decode:
            first_decode_barrier.wait(timeout=DECODE_TIMEOUT_S)
        return records

    monkeypatch.setattr(comports, "_decode_head", synchronize_first_decode)
    delivered: list[list[str]] = []
    failures: list[BaseException] = []

    def read_one_frame() -> None:
        try:
            records, _, _, _ = service._take_frames(session, frame_size, [], commit=True)
            delivered.append([record["message"] for record in records])
        except BaseException as error:
            failures.append(error)

    readers = [threading.Thread(target=read_one_frame, daemon=True) for _ in range(2)]
    started: list[threading.Thread] = []
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        for reader in started:
            reader.join(timeout=DECODE_TIMEOUT_S)
        if any(reader.is_alive() for reader in started):
            raise AssertionError("concurrent defmt readers did not finish")
        if failures:
            raise AssertionError("a concurrent defmt reader failed") from failures[0]

        messages = sorted(message for batch in delivered for message in batch)
        if messages != ["value 1", "value 2"]:
            raise AssertionError(EXPECTED_FAILURE)
        assert not session.buffer
    finally:
        first_decode_barrier.abort()
        for reader in started:
            reader.join(timeout=DECODE_TIMEOUT_S)
