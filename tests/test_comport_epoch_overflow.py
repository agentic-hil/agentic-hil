"""Synchronous coverage for COM buffer epochs during append and overflow."""

from __future__ import annotations

from defmt_wire import RzcobsEncoder, encode_frame

from agentic_hil import comports
from agentic_hil.comports import ComPortSession, DefmtStream
from agentic_hil.defmt import DefmtBinding, DefmtTable
from agentic_hil.types import ComPortConfig

MAX_BUFFER_BYTES = 64
OVERFLOW_FAILURE = "overflow trimming did not advance the snapshot epoch in the same step"


class ScriptedSerialHandle:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.session: ComPortSession | None = None
        self.states: list[tuple[int, int, int]] = []

    @property
    def in_waiting(self) -> int:
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, size: int) -> bytes:
        assert self.session is not None
        session = self.session
        with session.lock:
            snapshot = comports._defmt_snapshot(session, size)
            assert snapshot is not None
            self.states.append((len(session.buffer), snapshot[2], snapshot[3]))

        chunk = self.chunks.pop(0)
        assert len(chunk) <= size
        if not self.chunks:
            session.active = False
        return chunk


def new_session(chunks: list[bytes], max_buffer_bytes: int = MAX_BUFFER_BYTES) -> tuple[ComPortSession, ScriptedSerialHandle]:
    port_config = ComPortConfig(
        device="scripted",
        baudrate=115200,
        timeout_s=0.1,
        write_timeout_s=0.1,
        encoding="utf-8",
        max_buffer_bytes=max_buffer_bytes,
        max_write_bytes=64,
    )
    handle = ScriptedSerialHandle(chunks)
    session = ComPortSession("scripted", port_config, handle, "unused.jsonl", start_reader=False)
    handle.session = session
    session.defmt = DefmtStream(DefmtBinding(DefmtTable({}, None, "rzcobs", "4"), {}), None)
    return session, handle


def test_overflow_trims_advance_snapshot_epoch_with_overflow() -> None:
    session, handle = new_session([b"abcdef", b"ghij", b"KLMN"], max_buffer_bytes=8)

    session._drain_loop()

    with session.lock:
        final_snapshot = comports._defmt_snapshot(session, session.port_config.max_buffer_bytes)
    assert final_snapshot is not None
    states = [*handle.states, (len(final_snapshot[1]), final_snapshot[2], final_snapshot[3])]
    overflow_steps = [(before, after) for before, after in zip(states, states[1:], strict=False) if after[1] > before[1]]
    assert len(overflow_steps) == 2
    if any(after[2] != before[2] + 1 for before, after in overflow_steps):
        raise AssertionError(OVERFLOW_FAILURE)


def test_append_only_drain_preserves_snapshot_commit_and_tail(monkeypatch) -> None:
    table = DefmtTable({0: ("info", "Hello, world!")}, None, "rzcobs", "4")
    encoder = RzcobsEncoder()
    first = encoder.frame(encode_frame(table, "Hello, world!", []))
    tail = encoder.frame(encode_frame(table, "Hello, world!", []))
    session, handle = new_session([tail])
    session.defmt = DefmtStream(DefmtBinding(table, {}), None)
    session.buffer.extend(first)

    with session.lock:
        snapshot = comports._defmt_snapshot(session, len(first))
    assert snapshot is not None

    decode = comports._decode_head
    decode_calls = 0
    append_state = None

    def append_during_decode(*args):
        nonlocal append_state, decode_calls
        decode_calls += 1
        if decode_calls == 1:
            session._drain_loop()
            append_state = (session.overflow_bytes, session.buffer_epoch)
        return decode(*args)

    monkeypatch.setattr(comports, "_decode_head", append_during_decode)
    service = object.__new__(comports.ComPortService)
    records, consumed, matched, _ = service._take_frames(session, len(first), [], commit=True)

    assert append_state == (snapshot[2], snapshot[3])
    assert decode_calls == 1
    assert records
    assert matched is None
    assert consumed == len(first)
    assert bytes(session.buffer) == tail
