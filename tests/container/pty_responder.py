"""The scripted peer on the far end of a pseudo-terminal pair.

Test infrastructure for the product's serial transport, and only that. A
`socat` pair links two pseudo-terminals; the configuration under test names
one of them as its COM port, and this program holds the other. It reads what
the product wrote to its port, records every byte to a file the test reads
back, and answers each complete line out of the table it was started with,
after the delay it was given. It runs no firmware, models nothing electrical
and says nothing about any target: the table is the test's own input, written
beside the assertions that read the answers back, the way a fixture
configuration is.

It uses no pyserial and nothing from the product. What it exercises on the
product's side is pyserial's own POSIX behaviour against a real terminal
device: the exclusive open, the termios the open applies, the read that waits,
the write that lands on the wire. Those are the things the unit tier's fake
serial handle cannot show.

Usage:

    python pty_responder.py --device PATH --record PATH --ready PATH
        [--reply REQUEST=RESPONSE ...] [--delay-s SECONDS]

`REQUEST` is matched against each received line with its line ending removed;
`RESPONSE` is written verbatim. Both are read through Python's escape rules, so
a test writes `PING=PONG\\r\\n` and the bytes `PONG\r\n` go on the wire. A line
that matches nothing gets no answer. With no `--reply` at all the peer is
silent and only records, which is how a test proves a read timeout.

`--ready` names a file this writes once the device is open, so the test can
wait for the peer to be listening before it drives the product. The record
file is appended to as bytes arrive and flushed after every read, so a test
can read it while this is still running.
"""

from __future__ import annotations

import argparse
import codecs
import os
import select
import signal
import sys
import termios
import time
import tty


def _unescape(text: str) -> bytes:
    return codecs.decode(text, "unicode_escape").encode("latin-1")


def _parse_replies(raw: list[str]) -> dict[bytes, bytes]:
    table: dict[bytes, bytes] = {}
    for item in raw:
        request, separator, response = item.partition("=")
        if not separator:
            raise SystemExit(f"--reply takes REQUEST=RESPONSE, not {item!r}")
        table[_unescape(request)] = _unescape(response)
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", required=True, help="the pseudo-terminal this peer holds")
    parser.add_argument("--record", required=True, help="every byte received, appended as it arrives")
    parser.add_argument("--ready", required=True, help="written once the device is open")
    parser.add_argument("--reply", action="append", default=[], metavar="REQUEST=RESPONSE")
    parser.add_argument("--delay-s", type=float, default=0.0, help="wait this long before each answer")
    args = parser.parse_args(argv)
    replies = _parse_replies(args.reply)

    stop = False

    def on_signal(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY)
    try:
        # Raw, so a line ending is a byte and not a translation, and nothing
        # this peer receives is echoed back to the product as if it had been
        # answered. The pair was made raw by socat already; this is the peer's
        # own statement that it wants no line discipline of its own.
        tty.setraw(fd, termios.TCSANOW)
        with open(args.record, "ab") as record, open(args.ready, "w", encoding="utf-8") as ready:
            ready.write("ready\n")
            ready.flush()
            pending = b""
            while not stop:
                readable, _, _ = select.select([fd], [], [], 0.05)
                if not readable:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    # The pair is gone: socat was killed and the device with
                    # it. Nothing is left to answer.
                    break
                if not chunk:
                    break
                record.write(chunk)
                record.flush()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    answer = replies.get(line.rstrip(b"\r"))
                    if answer is None:
                        continue
                    if args.delay_s > 0:
                        time.sleep(args.delay_s)
                    written = 0
                    while written < len(answer):
                        written += os.write(fd, answer[written:])
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
