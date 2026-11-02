"""Flash each firmware variant through Agentic HIL and capture its UART report from the reset.

One bench run holds the debugger and the virtual COM port for all variants. Usage:
    run_variants.py <receipt dir> <image dir> <variant>...
"""

import json
import sys
from pathlib import Path

from remote_hil import Remote

UNTIL = ["RESULT pass", "RESULT fail", "RESULT panic"]


def main() -> int:
    audit, images, variants = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    remote = Remote(audit, timeout_s=180)
    summary: dict = {}
    try:
        start = remote.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"},
                                                            {"kind": "uart", "id": "dut"}]})
        print("bench_run_start:", start.get("ok"), start.get("error"), start.get("summary"))
        if not start.get("ok"):
            return 1
        for variant in variants:
            path = images / f"i2s7384-{variant}.elf"
            upload = remote.upload(path)
            print(variant, "upload:", upload.get("ok"), upload.get("artifact_id"), upload.get("error"))
            if not upload.get("ok"):
                summary[variant] = {"upload": upload}
                break
            flash = remote.call("flash_firmware", {
                "artifact_id": upload["artifact_id"], "reset_after_flash": True,
                "capture": {"port_id": "dut", "until": UNTIL, "wait_timeout_s": 40},
            })
            capture = flash.get("capture") or {}
            print(variant, "flash:", flash.get("ok"), flash.get("error"),
                  "| until_matched:", capture.get("until_matched"), capture.get("matched"))
            text = capture.get("text") or capture.get("output") or ""
            print(text if text else json.dumps(capture)[:1500])
            summary[variant] = {"artifact_id": upload["artifact_id"], "flash_ok": flash.get("ok"),
                                "until_matched": capture.get("until_matched")}
    finally:
        try:
            stop = remote.call("bench_run_stop")
            print("bench_run_stop:", stop.get("ok"), stop.get("released_devices"), stop.get("error"))
        finally:
            remote.close()
    (audit / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
