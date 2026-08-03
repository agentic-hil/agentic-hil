#!/usr/bin/env python3
"""A pyOCD that does not resolve the configured target type.

The refusal text is pyOCD 0.45.1's, verbatim, including the log prefix it writes
to stderr. Paraphrasing it would defeat the point: the shipped classifier matched
none of this string, so the remediation naming `pyocd pack install` never reached
the caller who needed it.
"""

from __future__ import annotations

import json
import sys

TARGET_NOT_RECOGNIZED = (
    "0001042 C Target type {target} not recognized. Use 'pyocd list --targets' to see currently available "
    "target types. See <https://pyocd.io/docs/target_support.html> for how to install additional target "
    "support. [__main__]"
)


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.45.1")
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--targets", "--no-config"]:
            # A pyOCD with no vendor pack installed: built-ins only.
            print(json.dumps({"pyocd_version": "0.45.1", "status": 0, "targets": [{"name": "cortex_m", "vendor": "Generic", "part_number": "CoreSightTarget", "source": "builtin"}]}))
            return 0
        if args[1:] == ["--probes", "--no-config"]:
            print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
            return 0
        print("unsafe probe discovery arguments", file=sys.stderr)
        return 2
    target = args[args.index("--target") + 1] if "--target" in args else "<unset>"
    print(TARGET_NOT_RECOGNIZED.format(target=target), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
