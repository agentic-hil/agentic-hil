#!/bin/sh
# The whole TLS-proxy bench scenario, start to finish, in one run.
#
# Seed:    install agentic-hil 0.16.0 through the proxy with UV_SYSTEM_CERTS=1.
#          This provisions the bench and proves the system-store path works.
# Proof 1: `agentic-hil upgrade --json` without that variable fails, and the
#          report says so with uv's own certificate error inside it. This is the
#          released 0.16.0, from before the retry, so it fails and stays failed.
# Proof 2: the upgrade in *this checkout* is placed into a real uv tool
#          environment and run on the same proxy. Its first attempt meets the
#          same failure, and its own retry against this machine's store gets past
#          it -- the code under review, exercised live rather than through a stub.
# Proof 3: the released one-line installer meets the same failure, recognises
#          it, switches to this machine's own store and finishes the install.
#
# Every step's decisive output is printed whole. A line that is filtered out is
# a line the next reader has to reproduce for themselves, and the certificate
# error is the entire point of this container.
#
# The first assertion that fails ends the run non-zero. Later proofs depend on
# earlier ones (proof 1 needs the seeded 0.16.0, proof 2 reprovisions it
# unpinned, proof 3 replaces it), so there is nothing to learn from continuing
# past a failure.

set -u

# The release the bench runs. Pinned exactly, because the whole run is read
# against it: the seed installs it, proof 1 fails from it, proof 2 has to move
# off it.
SEED_VERSION=0.16.0
SEED_REQUIREMENT="agentic-hil==$SEED_VERSION"
RELEASES_LATEST=https://github.com/agentic-hil/agentic-hil/releases/latest
INSTALLER_URL="$RELEASES_LATEST/download/install.sh"

PROXY_LOG=/tmp/mitmdump.log
SEED_LOG=/tmp/seed.log
UPGRADE_REPORT=/tmp/upgrade.json
UPGRADE_ERRORS=/tmp/upgrade.err
INSTALLER_LOG=/tmp/installer.log

# The package under review, copied into the image at build time so proof 2 can
# run this checkout's own upgrade on the bench rather than a released artifact.
CANDIDATE_SRC=/opt/tls-proxy/candidate/agentic_hil
CANDIDATE_SEED_LOG=/tmp/candidate-seed.log
CANDIDATE_REPORT=/tmp/candidate-upgrade.json
CANDIDATE_ERRORS=/tmp/candidate-upgrade.err

PROXY_PID=""

heading() {
    printf '\n========== %s ==========\n' "$1"
}

# A step's verdict, and under it the one line a reader needs to believe it.
pass() {
    printf 'PASS: %s\n' "$1"
    printf '      %s\n' "$2"
}

fail() {
    printf 'FAIL: %s\n' "$1"
    printf '      %s\n' "$2"
    stop_proxy
    exit 1
}

# The bench never came up, so no proof ran. Distinct from a failed proof, and
# non-zero all the same.
abort() {
    printf 'SETUP FAILED: %s\n' "$1"
    stop_proxy
    exit 2
}

verbatim() {
    printf '%s\n' "--- $1 ---"
    cat "$2"
    printf '%s\n' "--- end $1 ---"
}

# One field out of the upgrade report, by dotted path, empty when it is absent.
# raw_decode from the first brace, so a future line of prose before or after the
# document cannot turn a real failure into a parse error nobody can read.
report_field() {
    python3 -c '
import json
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
start = text.find("{")
if start < 0:
    sys.exit(0)
try:
    document, _ = json.JSONDecoder().raw_decode(text[start:])
except ValueError:
    sys.exit(0)
value = document
for key in sys.argv[2].split("."):
    if not isinstance(value, dict) or key not in value:
        sys.exit(0)
    value = value[key]
print(value if isinstance(value, str) else json.dumps(value))
' "$1" "$2" 2>/dev/null
}

contains() {
    grep -q -F -- "$2" "$1"
}

stop_proxy() {
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null
        PROXY_PID=""
    fi
}

start_proxy() {
    mitmdump --quiet \
        --set confdir="$MITMPROXY_CONFDIR" \
        --listen-host "$TLS_PROXY_HOST" \
        --listen-port "$TLS_PROXY_PORT" \
        >"$PROXY_LOG" 2>&1 &
    PROXY_PID=$!
    waited=0
    while [ "$waited" -lt 60 ]; do
        if python3 -c '
import socket
import sys

probe = socket.socket()
probe.settimeout(1)
sys.exit(probe.connect_ex((sys.argv[1], int(sys.argv[2]))))
' "$TLS_PROXY_HOST" "$TLS_PROXY_PORT" 2>/dev/null; then
            printf 'proxy: mitmdump is listening on %s:%s, and this machine trusts its CA\n' \
                "$TLS_PROXY_HOST" "$TLS_PROXY_PORT"
            return 0
        fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    verbatim "mitmdump output" "$PROXY_LOG"
    abort "mitmdump never accepted a connection on $TLS_PROXY_HOST:$TLS_PROXY_PORT"
}

heading "The bench"
printf 'proxy environment: HTTPS_PROXY=%s HTTP_PROXY=%s\n' "$HTTPS_PROXY" "$HTTP_PROXY"
# Named in every transcript, because uv is the program whose trust decision this
# whole run is about and its wording is what the installer's detection matches.
printf 'package manager: %s\n' "$(uv --version)"
start_proxy

# curl reads this machine's own store, which now holds the proxy's CA. It is the
# control: the interception is invisible to everything that trusts the machine,
# which is why the operator on such a bench sees only uv fail.
if ! curl -sSf -o /dev/null https://pypi.org/simple/agentic-hil/ 2>/tmp/curl.err; then
    verbatim "curl error" /tmp/curl.err
    abort "curl could not reach the index through the proxy, so this container is not the bench it claims to be"
fi
printf '%s\n' "control: curl reaches https://pypi.org/simple/agentic-hil/ through the proxy, because it reads this machine's own store"

heading "Seed: install agentic-hil $SEED_VERSION through the proxy, against this machine's own store"
printf 'command: UV_SYSTEM_CERTS=1 uv tool install "%s"\n' "$SEED_REQUIREMENT"
if UV_SYSTEM_CERTS=1 uv tool install "$SEED_REQUIREMENT" >"$SEED_LOG" 2>&1; then
    verbatim "uv output" "$SEED_LOG"
else
    verbatim "uv output" "$SEED_LOG"
    fail "seed, uv could not install $SEED_REQUIREMENT even with UV_SYSTEM_CERTS=1" \
        "the system store does not carry the proxy CA this image installed, so nothing below can be measured"
fi

seeded=$(agentic-hil --version 2>/dev/null)
if [ "$seeded" != "$SEED_VERSION" ]; then
    fail "seed, the bench does not run $SEED_VERSION" \
        "agentic-hil --version answered '$seeded'"
fi
pass "seed, agentic-hil $SEED_VERSION installed through the proxy with UV_SYSTEM_CERTS=1" \
    "agentic-hil --version: $seeded"

heading "Proof 1: agentic-hil upgrade on the bench as the operator finds it"
printf 'command: agentic-hil upgrade --json, with UV_SYSTEM_CERTS unset\n'
env -u UV_SYSTEM_CERTS agentic-hil upgrade --json >"$UPGRADE_REPORT" 2>"$UPGRADE_ERRORS"
upgrade_status=$?
verbatim "upgrade report" "$UPGRADE_REPORT"
if [ -s "$UPGRADE_ERRORS" ]; then
    verbatim "upgrade stderr" "$UPGRADE_ERRORS"
fi
printf 'exit status: %s\n' "$upgrade_status"

error_type=$(report_field "$UPGRADE_REPORT" error_type)
if [ "$error_type" != "upgrade_failed" ]; then
    fail "proof 1, the upgrade did not fail the way this bench fails" \
        "the report carries error_type '$error_type', not 'upgrade_failed'"
fi

report_field "$UPGRADE_REPORT" install.stderr >/tmp/manager.err
verbatim "uv stderr as the report recorded it" /tmp/manager.err
for marker in 'invalid peer certificate' 'UnknownIssuer'; do
    if ! contains /tmp/manager.err "$marker"; then
        fail "proof 1, the recorded manager stderr does not carry '$marker'" \
            "this failure is not the trust failure this eval reproduces"
    fi
done

decisive=$(grep -F -- 'invalid peer certificate' /tmp/manager.err | head -n 1 | sed 's/^[[:space:]]*//')
pass "proof 1, agentic-hil upgrade fails against uv's own bundled roots" \
    "$decisive"

heading "Proof 2: the upgrade under review heals itself against this machine's own store"
# Proof 1 is the released 0.16.0, which has no retry and cannot grow one. This
# proof is the code in this checkout, run on the same bench, so the retry meets
# real uv, a real child environment and this machine's real trust store rather
# than the stub tests/test_upgrade_certificates.py holds.
#
# It is provisioned by name and unpinned, on purpose. `uv tool upgrade
# agentic-hil` then fetches the index and meets the very trust failure proof 1
# just recorded -- the one mechanism this bench is known to reproduce. A path
# install would rebuild agentic-hil from disk and might never reach the index,
# and the pinned 0.16.0 seed would send the retry past the failure only to be
# told there was nothing to move.
printf 'command: UV_SYSTEM_CERTS=1 uv tool install --force agentic-hil\n'
if UV_SYSTEM_CERTS=1 uv tool install --force agentic-hil >"$CANDIDATE_SEED_LOG" 2>&1; then
    verbatim "uv output" "$CANDIDATE_SEED_LOG"
else
    verbatim "uv output" "$CANDIDATE_SEED_LOG"
    fail "proof 2, the candidate environment could not be provisioned through the proxy" \
        "with no installed tool to overlay there is nothing for this checkout's upgrade to run out of"
fi

# The tool's own interpreter names where the installed package sits. It is asked
# for through `uv tool dir` rather than read off a console-script shebang, which
# uv rewrites into a /bin/sh trampoline once the path is long enough. This
# checkout is overlaid onto that package -- every module overwritten, any
# release-only data left in place -- and the stale bytecode dropped, because uv
# installs hash-pinned .pyc files an mtime would never invalidate and the old
# code would otherwise keep running.
tool_python="$(uv tool dir)/agentic-hil/bin/python"
if [ ! -x "$tool_python" ]; then
    fail "proof 2, the tool interpreter is not where uv tool dir says it is" \
        "looked for $tool_python, and without it this checkout's code cannot be placed on the bench"
fi
candidate_target=$("$tool_python" -c 'import agentic_hil, os; print(os.path.dirname(agentic_hil.__file__))' 2>/dev/null)
if [ -z "$candidate_target" ] || [ ! -d "$candidate_target" ]; then
    fail "proof 2, the installed package directory could not be located" \
        "the overlay had no target, so this checkout's code was never placed on the bench"
fi
cp -R "$CANDIDATE_SRC/." "$candidate_target/"
"$tool_python" -c 'import pathlib, shutil, sys
for cache in pathlib.Path(sys.argv[1]).rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)' "$candidate_target"
overlaid=$("$tool_python" -c 'import agentic_hil; print(agentic_hil.__version__)' 2>/dev/null)
printf 'overlay: the installed agentic_hil now reports %s, the version in this checkout\n' "$overlaid"

printf 'command: agentic-hil upgrade --json, with UV_SYSTEM_CERTS unset\n'
env -u UV_SYSTEM_CERTS agentic-hil upgrade --json >"$CANDIDATE_REPORT" 2>"$CANDIDATE_ERRORS"
candidate_status=$?
verbatim "upgrade report" "$CANDIDATE_REPORT"
if [ -s "$CANDIDATE_ERRORS" ]; then
    verbatim "upgrade stderr" "$CANDIDATE_ERRORS"
fi
printf 'exit status: %s\n' "$candidate_status"

# The retry keeps the attempt that named the proxy under
# install.certificate_retry.first_attempt rather than dropping it. Its presence
# at all is proof this checkout's code ran: 0.16.0 has no such field. Both halves
# of the signature have to be in it, the same two proof 1 asserted.
report_field "$CANDIDATE_REPORT" install.certificate_retry.first_attempt.stderr >/tmp/candidate-first.err
verbatim "first attempt stderr, as the retry recorded it" /tmp/candidate-first.err
for marker in 'invalid peer certificate' 'UnknownIssuer'; do
    if ! contains /tmp/candidate-first.err "$marker"; then
        fail "proof 2, the first attempt's recorded stderr does not carry '$marker'" \
            "the upgrade under review did not begin from the trust failure this bench produces"
    fi
done

retry_variable=$(report_field "$CANDIDATE_REPORT" install.certificate_retry.variable)
retry_value=$(report_field "$CANDIDATE_REPORT" install.certificate_retry.value)
if [ "$retry_variable" != "UV_SYSTEM_CERTS" ] || [ "$retry_value" != "1" ]; then
    fail "proof 2, the retry did not reach for this machine's own store" \
        "expected UV_SYSTEM_CERTS=1 in the retry, the report carries '$retry_variable=$retry_value'"
fi

candidate_ok=$(report_field "$CANDIDATE_REPORT" ok)
if [ "$candidate_ok" != "true" ]; then
    fail "proof 2, the upgrade under review did not succeed against this machine's own store" \
        "the report carries ok '$candidate_ok', so the retry never got past the trust failure"
fi

candidate_note=$(report_field "$CANDIDATE_REPORT" certificates)
if ! printf '%s\n' "$candidate_note" | grep -q -F -- 'succeeded'; then
    fail "proof 2, the result does not say the retry is the attempt that succeeded" \
        "certificates carries: $candidate_note"
fi

pass "proof 2, the upgrade under review met the trust failure and healed itself against this machine's own store" \
    "$candidate_note"

heading "Proof 3: the released one-line installer, on the same bench, unchanged"
printf 'command: curl -LsSf %s | sh\n' "$INSTALLER_URL"
curl -LsSf "$INSTALLER_URL" | sh >"$INSTALLER_LOG" 2>&1
installer_status=$?
verbatim "installer output" "$INSTALLER_LOG"
printf 'exit status: %s\n' "$installer_status"

if [ "$installer_status" -ne 0 ]; then
    fail "proof 3, the released installer exited $installer_status" \
        "the anchor did not repair this bench"
fi
if ! contains "$INSTALLER_LOG" 'invalid peer certificate'; then
    fail "proof 3, the installer never met the trust failure" \
        "without the failure there is nothing for it to detect, so this run proves nothing"
fi
switch="retrying once against this machine's own store"
if ! contains "$INSTALLER_LOG" "$switch"; then
    fail "proof 3, the installer did not say it was switching to this machine's own store" \
        "expected a line containing: $switch"
fi

released_tag=$(curl -sSL -o /dev/null -w '%{url_effective}' "$RELEASES_LATEST")
released_version=${released_tag##*/}
released_version=${released_version#v}
installed=$(agentic-hil --version 2>/dev/null)
if [ -z "$released_version" ] || [ "$installed" != "$released_version" ]; then
    fail "proof 3, the bench does not run the released version" \
        "agentic-hil --version answered '$installed', the latest release is '$released_version'"
fi
if [ "$installed" = "$SEED_VERSION" ]; then
    fail "proof 3, the installation never moved off the seeded version" \
        "agentic-hil --version still answers $SEED_VERSION"
fi

detection=$(grep -F -- "$switch" "$INSTALLER_LOG" | head -n 1)
pass "proof 3, the released installer detected the trust failure and finished against this machine's own store" \
    "$detection"
printf '      agentic-hil --version: %s (the latest release)\n' "$installed"

heading "Result"
printf 'Reproduced on one bench: the released 0.16.0 upgrade fails on a TLS-inspecting\n'
printf 'proxy, the upgrade in this checkout retries against the machine own store and\n'
printf 'gets through, and the released one-line installer does the same for an operator\n'
printf 'still on a version from before that retry.\n'
stop_proxy
exit 0
