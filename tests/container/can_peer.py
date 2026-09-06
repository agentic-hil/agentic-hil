"""The far end of a virtual CAN interface, answering frames from a table the test wrote.

This is test infrastructure for the product's SocketCAN transport and nothing
more. It is a second CAN_RAW socket on the same virtual interface the product
binds, and what it does is decided entirely by the `--reply` rules on its
command line: a frame that matches a rule's identifier and payload is answered
with the rule's reply, every other frame is ignored. Those rules are the test's
own input, documented beside the plan that reads them, the way a fixture
configuration is.

It runs no firmware, models no controller and no electrical behaviour, and no
test describes it as a board. What it lets a test prove is that a frame the
product sent reached a second socket on the interface as the frame the plan
wrote, and that a frame a second socket sent reached the product's read path
and its comparator. Everything that needs a controller on a wire stays on the
bench.

Usage::

    python can_peer.py --channel vcan0 --ready /tmp/peer-ready \\
        --reply 0x123/01=0x124/02

`--reply ID/DATA=ID/DATA`: identifiers in hexadecimal, payloads as hexadecimal
bytes, an empty payload written as an empty string (`0x200/=0x201/ff`). All
frames are standard (11-bit) frames. `--ready` names a file written once the
socket is bound, which is when a test may start sending. The process runs until
it is terminated.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path


def parse_rule(text: str) -> tuple[tuple[int, bytes], tuple[int, bytes]]:
    """`0x123/01ff=0x124/02` into ((0x123, b'\\x01\\xff'), (0x124, b'\\x02'))."""
    try:
        heard, answer = text.split("=", 1)
        heard_id, heard_data = heard.split("/", 1)
        answer_id, answer_data = answer.split("/", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"a reply rule is ID/DATA=ID/DATA, not {text!r}") from error
    return (int(heard_id, 16), bytes.fromhex(heard_data)), (int(answer_id, 16), bytes.fromhex(answer_data))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channel", required=True, help="the SocketCAN interface to bind, for example vcan0")
    parser.add_argument("--ready", required=True, help="a file written once the socket is bound")
    parser.add_argument("--reply", action="append", default=[], type=parse_rule, metavar="ID/DATA=ID/DATA", help="answer a matching standard frame with this one; repeatable")
    arguments = parser.parse_args(argv)

    import can

    running = True

    def stop(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    bus = can.Bus(interface="socketcan", channel=arguments.channel)
    try:
        Path(arguments.ready).write_text("bound\n", encoding="utf-8")
        rules = dict(arguments.reply)
        while running:
            message = bus.recv(timeout=0.2)
            if message is None or message.is_extended_id or message.is_remote_frame:
                continue
            answer = rules.get((message.arbitration_id, bytes(message.data)))
            if answer is None:
                continue
            bus.send(can.Message(arbitration_id=answer[0], data=answer[1], is_extended_id=False), timeout=1.0)
    finally:
        bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
