#!/bin/sh
# What this script touches, and nothing else:
#   1. the agentic-hil package, installed user-local (uv tool, or pip --user).
#   2. your agent's skill file, under your own home directory.
#   3. your agent's user-level MCP registration, under your own home directory.
#   4. nothing in any repository, no project configuration, no shell rc file.
#   5. nothing that needs administrator rights: no sudo, no system package manager.

set -eu

# The release this script installs, and the version an installation already
# here has to reach to be left alone. Deliberately not a capability floor: the
# line that installs Agentic HIL is the same line people re-run to get current,
# and step 4 registers the skill out of whatever copy step 1 decided to keep, so
# a floor left a returning user on an old package and an old skill at once. A
# development tree reports X.Y.Z.devN, which compares as X.Y.Z and stays put.
RELEASE="0.16.0"

AGENT=""
WITH_AGENT_INSTALL=1
PINNED=""
WITH_CAN=1
# auto, always or never: whether this machine's own certificate store is
# reached for, and whether it takes a failed attempt first.
SYSTEM_CERTS="auto"
STEP_TOTAL=5

say() {
    printf 'agentic-hil install: %s\n' "$1"
}

step() {
    printf 'agentic-hil install: step %s/%s  %s\n' "$1" "$STEP_TOTAL" "$2"
}

fail() {
    printf 'agentic-hil install: %s\n' "$1" >&2
    exit 1
}

have() {
    command -v "$1" >/dev/null 2>&1
}

usage() {
    cat <<'USAGE'
Usage: install.sh [options]
       curl -LsSf https://agentic-hil.github.io/install.sh | sh
       curl -LsSf https://agentic-hil.github.io/install.sh | sh -s -- --agent claude

Installs Agentic HIL user-local and registers the skill and the MCP server for
your AI agent. It writes no project configuration and asks for no admin rights.

Options:
  --agent <name>      Register for this agent only: claude-code, codex or
                      opencode. Without it, every agent CLI found on PATH is
                      registered, and no agent CLI is installed for you.
  --no-agent-install  Install the package and stop there. Nothing belonging to
                      an agent is written.
  --version <x.y.z>   Install exactly this release, as agentic-hil==x.y.z.
                      Later upgrades go through "agentic-hil upgrade", which
                      keeps the extras and the manager that owns the
                      installation, not through a second run with a new pin.
  --can               Install the [can] extra for PEAK and SocketCAN adapters.
                      This is the default.
  --no-can            Install without the [can] extra.
  --system-certs      Validate TLS against this machine's own certificate
                      store, the one curl and apt already read, from the start.
                      Rarely needed: a failure that carries the signature of a
                      TLS-intercepting proxy is retried against that store by
                      itself. This only skips the first attempt.
  --no-system-certs   Never reach for this machine's store, not even after such
                      a failure. The install fails instead.
                      Neither flag disables verification. Nothing here does.
  --help              Print this text and exit.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --agent)
            if [ $# -lt 2 ]; then
                fail "--agent needs an agent name"
            fi
            AGENT="$2"
            shift 2
            ;;
        --agent=*)
            AGENT="${1#--agent=}"
            shift
            ;;
        --no-agent-install)
            WITH_AGENT_INSTALL=0
            shift
            ;;
        --version)
            if [ $# -lt 2 ]; then
                fail "--version needs a release number"
            fi
            PINNED="$2"
            shift 2
            ;;
        --version=*)
            PINNED="${1#--version=}"
            shift
            ;;
        --can)
            WITH_CAN=1
            shift
            ;;
        --no-can)
            WITH_CAN=0
            shift
            ;;
        --system-certs)
            SYSTEM_CERTS="always"
            shift
            ;;
        --no-system-certs)
            SYSTEM_CERTS="never"
            shift
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            printf 'agentic-hil install: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# The certificate store a TLS-intercepting proxy needs, and the only concession
# this script makes to one. uv validates against roots bundled in its own
# binary, so on a managed network it fails where curl and apt on the same host
# keep working, which is what makes the cause recognisable; this points it at
# the store those two already read. pip takes a file rather than a switch, so
# the usual locations are tried and whichever is found is named.
#
# Nobody has to know any of that in advance. The failed install is the
# detection: a package manager that came back with one of the signatures below
# is retried once against this machine's store, and only that. Verification is
# on in both attempts, and this is not a way to reach a switch that turns it
# off: --allow-insecure-host, --insecure and --trusted-host are not options this
# script offers, and no path through it arrives at one.
system_cert_bundle() {
    for candidate in \
        /etc/ssl/certs/ca-certificates.crt \
        /etc/pki/tls/certs/ca-bundle.crt \
        /etc/ssl/ca-bundle.pem \
        /etc/ssl/cert.pem; do
        if [ -r "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

# What a chain that ends outside the manager's own roots looks like, from uv
# (rustls), pip (OpenSSL) and curl. A failure that says none of this is a
# failure about something else, and is never retried.
trust_failure() {
    case "$1" in
        *"invalid peer certificate"* | *UnknownIssuer* | *"self-signed certificate"* | *"self signed certificate"* | *"certificate verify failed"* | *CERTIFICATE_VERIFY_FAILED* | *"unable to get local issuer certificate"*)
            return 0
            ;;
    esac
    return 1
}

use_system_certs() {
    UV_SYSTEM_CERTS=1
    export UV_SYSTEM_CERTS
    if [ -n "${PIP_CERT:-}" ]; then
        say "certificates: uv reads this machine's own store; pip keeps the PIP_CERT already set here"
    elif pip_cert_bundle=$(system_cert_bundle); then
        PIP_CERT="$pip_cert_bundle"
        export PIP_CERT
        say "certificates: uv and pip read this machine's own store ($pip_cert_bundle)"
    else
        say "certificates: uv reads this machine's own store; no bundle file was found here for pip, so set PIP_CERT to one if pip is what fails"
    fi
}

# Whether a PIP_CERT already in the environment names this machine's own store
# rather than a bundle the operator chose for themselves. The list is the one
# system_cert_bundle reaches for, so an inherited PIP_CERT that equals any of
# those paths is the same reach --no-system-certs forbids.
is_system_cert_bundle() {
    for candidate in \
        /etc/ssl/certs/ca-certificates.crt \
        /etc/pki/tls/certs/ca-bundle.crt \
        /etc/ssl/ca-bundle.pem \
        /etc/ssl/cert.pem; do
        if [ "$1" = "$candidate" ]; then
            return 0
        fi
    done
    return 1
}

# --no-system-certs promises never to reach for this machine's own store, and it
# can only keep that promise by clearing an inherited override as well as by not
# setting one. TROUBLESHOOTING.md recommends exporting UV_SYSTEM_CERTS so future
# upgrades keep working behind a proxy, so an operator who then passes
# --no-system-certs would still have uv reading the machine store from that
# variable. A PIP_CERT the operator aimed at a bundle of their own is their
# choice and stays; one that already points at this machine's system bundle is
# the same reach the flag refuses, and goes with UV_SYSTEM_CERTS.
clear_system_certs() {
    if [ -n "${UV_SYSTEM_CERTS:-}" ]; then
        unset UV_SYSTEM_CERTS
        say "certificates: --no-system-certs; cleared the inherited UV_SYSTEM_CERTS so uv does not read this machine's own store"
    fi
    if [ -n "${PIP_CERT:-}" ] && is_system_cert_bundle "${PIP_CERT}"; then
        unset PIP_CERT
        say "certificates: --no-system-certs; cleared the inherited system-bundle PIP_CERT so pip does not read this machine's own store"
    fi
}

if [ "$SYSTEM_CERTS" = "always" ]; then
    use_system_certs
elif [ "$SYSTEM_CERTS" = "never" ]; then
    clear_system_certs
fi

# The leading run of digits of one dot-separated field, so a development version
# spelled X.Y.Z.devN compares as X.Y.Z instead of refusing to parse.
numeric_prefix() {
    value="$1"
    digits=""
    while [ -n "$value" ]; do
        head_char=${value%"${value#?}"}
        case "$head_char" in
            [0-9]) digits="${digits}${head_char}" ;;
            *) break ;;
        esac
        value=${value#?}
    done
    if [ -z "$digits" ]; then
        digits=0
    fi
    printf '%s' "$digits"
}

version_part() {
    rest="$1"
    wanted="$2"
    n=1
    while [ "$n" -lt "$wanted" ]; do
        case "$rest" in
            *.*) rest=${rest#*.} ;;
            *) rest="" ;;
        esac
        n=$((n + 1))
    done
    numeric_prefix "${rest%%.*}"
}

version_at_least() {
    index=1
    while [ "$index" -le 3 ]; do
        found_part=$(version_part "$1" "$index")
        floor_part=$(version_part "$2" "$index")
        if [ "$found_part" -gt "$floor_part" ]; then
            return 0
        fi
        if [ "$found_part" -lt "$floor_part" ]; then
            return 1
        fi
        index=$((index + 1))
    done
    return 0
}

# Every one of the three numeric fields equal: the proof, for an explicit
# --version pin, that a reported version is the pinned release and not merely a
# newer one that happens to sit at least as high.
version_exactly() {
    index=1
    while [ "$index" -le 3 ]; do
        found_part=$(version_part "$1" "$index")
        want_part=$(version_part "$2" "$index")
        if [ "$found_part" -ne "$want_part" ]; then
            return 1
        fi
        index=$((index + 1))
    done
    return 0
}

python_is_new_enough() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
    for candidate in python3 python py; do
        if have "$candidate" && python_is_new_enough "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

package_spec() {
    spec="agentic-hil"
    if [ "$WITH_CAN" -eq 1 ]; then
        spec="agentic-hil[can]"
    fi
    if [ -n "$PINNED" ]; then
        spec="${spec}==${PINNED}"
    fi
    printf '%s' "$spec"
}

# Does a freshly installed copy's reported version answer what this run asked
# for: exactly the pin when one was given, at least the release otherwise. The pin
# must match exactly -- a newer copy left in the manager's bin is not the pinned
# release this run wrote, and the documented --version contract is an exact
# release, not a floor. Without a pin the manager only ever writes the newest, so
# any copy at or above the release is the one just installed.
version_matches_request() {
    if [ -n "$PINNED" ]; then
        version_exactly "$1" "$PINNED"
    else
        version_at_least "$1" "$RELEASE"
    fi
}

# The one release of Astral's uv installer this script is allowed to run, and
# the SHA-256 of exactly those bytes. They are one pair: bumping the version
# without the hash makes every install fail, bumping the hash without the version
# makes every install fail, and that is the point. Refreshing them is a release
# chore, written down in docs/release-strategy.md, not something an install
# decides on the operator's machine.
UV_INSTALLER_VERSION="0.12.5"
UV_INSTALLER_SHA256="504511fbbbd811aeaba6738abc79408956b6c7da0ca35437b3dcc24a41efc111"

sha256_of() {
    # The first checksum tool this machine actually has. GNU coreutils spells it
    # sha256sum, macOS ships shasum, and openssl answers where neither is
    # installed. The first two print the digest first, openssl prints it last.
    if have sha256sum; then
        digest=$(sha256sum "$1") || return 1
        printf '%s' "${digest%% *}"
    elif have shasum; then
        digest=$(shasum -a 256 "$1") || return 1
        printf '%s' "${digest%% *}"
    elif have openssl; then
        digest=$(openssl dgst -sha256 "$1") || return 1
        printf '%s' "${digest##* }"
    else
        return 1
    fi
}

fetch_uv() {
    # Astral's own installer for uv, pinned to one release and checked before a
    # line of it runs. Two decisions are worth keeping visible here.
    #
    # Why the pin. The moving https://astral.sh/uv/install.sh serves whatever is
    # current at the second it is asked, so piping it into sh executes bytes
    # nobody here has ever read, on a machine that has just been told this script
    # touches five things and nothing else. Every other hop in the chain is
    # already pinned: this script is published with its own .sha256 beside it,
    # and the package comes hash-checked from PyPI. The versioned URL names one
    # release, the constant above names its exact bytes, and a download that is
    # not those bytes is not run at all.
    #
    # Why their checksums stay the second layer. This pin vouches for the
    # installer, not for the uv binaries it goes on to fetch. Astral's POSIX
    # installer carries a SHA-256 per release artifact and verifies the archive
    # it downloaded against it, which is the hop this pin cannot reach. Neither
    # layer replaces the other: ours proves the script, theirs proves the binary.
    url="https://astral.sh/uv/${UV_INSTALLER_VERSION}/install.sh"
    if ! have mktemp; then
        say "package: no mktemp here, so the pinned uv installer cannot be downloaded to a private file"
        return 1
    fi
    if ! have sha256sum && ! have shasum && ! have openssl; then
        say "package: no sha256sum, shasum or openssl here, so the pinned uv installer cannot be verified"
        return 1
    fi
    installer_path=$(mktemp) || return 1
    if have curl; then
        curl -LsSf "$url" -o "$installer_path" || { rm -f "$installer_path"; return 1; }
    elif have wget; then
        wget -qO "$installer_path" "$url" || { rm -f "$installer_path"; return 1; }
    else
        rm -f "$installer_path"
        return 1
    fi
    found_hash=$(sha256_of "$installer_path") || { rm -f "$installer_path"; return 1; }
    if [ "$found_hash" != "$UV_INSTALLER_SHA256" ]; then
        rm -f "$installer_path"
        printf 'agentic-hil install: the pinned uv installer does not match its recorded hash, so it was not run.\n' >&2
        printf 'agentic-hil install:   url      %s\n' "$url" >&2
        printf 'agentic-hil install:   expected %s\n' "$UV_INSTALLER_SHA256" >&2
        printf 'agentic-hil install:   found    %s\n' "$found_hash" >&2
        fail "the pin in this script may be stale: check for a newer uv release, then refresh UV_INSTALLER_VERSION and UV_INSTALLER_SHA256 together. Until then, install uv or Python 3.10 or newer yourself and run this again."
    fi
    if sh "$installer_path"; then
        rm -f "$installer_path"
        return 0
    fi
    rm -f "$installer_path"
    return 1
}

user_bin_on_path() {
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    export PATH
}

# uv's output is captured rather than streamed, because the text of a failure is
# what decides whether there is a second attempt to make.
install_with_uv() {
    if uv_output=$(uv tool install --upgrade "$(package_spec)" 2>&1); then
        printf '%s\n' "$uv_output"
        return 0
    fi
    printf '%s\n' "$uv_output" >&2
    if [ "$SYSTEM_CERTS" = "auto" ] && trust_failure "$uv_output"; then
        say "certificates: that is a certificate uv cannot get to a root it carries, which is what a TLS-intercepting proxy looks like from inside uv; retrying once against this machine's own store, with verification still on"
        use_system_certs
        SYSTEM_CERTS="always"
        if uv_output=$(uv tool install --upgrade "$(package_spec)" 2>&1); then
            printf '%s\n' "$uv_output"
            return 0
        fi
        printf '%s\n' "$uv_output" >&2
        fail "package: uv could not install $(package_spec) against this machine's own store either; the proxy's own CA is missing from that store, and installing it there is the fix; TROUBLESHOOTING.md section 1 has the rest"
    fi
    if [ "$SYSTEM_CERTS" = "never" ] && trust_failure "$uv_output"; then
        fail "package: uv could not install $(package_spec), and that is a certificate failure this run was told not to retry against this machine's own store; drop --no-system-certs, or install the proxy's CA where uv can be pointed at it; TROUBLESHOOTING.md section 1 has the rest"
    fi
    fail "package: uv could not install $(package_spec); TROUBLESHOOTING.md section 1 has the fallbacks"
}

install_with_pip() {
    python_bin="$1"
    if pip_output=$("$python_bin" -m pip install --user --upgrade "$(package_spec)" 2>&1); then
        printf '%s\n' "$pip_output"
        return 0
    fi
    printf '%s\n' "$pip_output" >&2
    if [ "$SYSTEM_CERTS" = "auto" ] && trust_failure "$pip_output"; then
        use_system_certs
        SYSTEM_CERTS="always"
        if [ -n "${PIP_CERT:-}" ]; then
            say "certificates: retrying pip once against this machine's own store, with verification still on"
            if pip_output=$("$python_bin" -m pip install --user --upgrade "$(package_spec)" 2>&1); then
                printf '%s\n' "$pip_output"
                return 0
            fi
            printf '%s\n' "$pip_output" >&2
        fi
    fi
    case "$pip_output" in
        *externally-managed-environment* | *"externally managed"*)
            # PEP 668: the distribution owns this interpreter. The answer is an
            # environment of uv's own, never --break-system-packages.
            return 3
            ;;
        *)
            return 1
            ;;
    esac
}

# Which manager step 2 installed with, so step 3 can ask that manager where it
# actually put the executable rather than guess.
PACKAGE_MANAGER=""

manager_bin_dir() {
    # The one directory the manager step 2 used just wrote the console script
    # into, asked of that manager itself and nothing guessed. For uv that is
    # `uv tool dir --bin`, which honours UV_TOOL_BIN_DIR and the XDG data
    # directory, neither of which a guessed user bin can name; for pip --user it
    # is the selected interpreter's own user scripts path. A copy in any other
    # directory cannot answer for this install, so when the manager cannot name
    # its destination this prints nothing and the caller fails rather than
    # scanning a directory that may hold a stale copy earlier on PATH.
    case "${PACKAGE_MANAGER:-}" in
        uv)
            uv tool dir --bin 2>/dev/null || true
            ;;
        pip)
            if [ -n "${DISCOVERED_PYTHON:-}" ]; then
                "$DISCOVERED_PYTHON" -c 'import sysconfig; print(sysconfig.get_path("scripts", "posix_user"))' 2>/dev/null || true
            fi
            ;;
    esac
}

# The exact agentic-hil the machine half calls. It stays the bare name only when
# step 1 found a new-enough copy already here and installed nothing; once this
# run installs a copy, it becomes that copy's own path, so an older agentic-hil
# earlier on PATH cannot answer for the install that just happened.
AGENTIC_HIL_CMD="agentic-hil"

installed_executable_dir() {
    # The manager's own destination directory, and only that. The copy there must
    # answer and match what this run asked for -- exactly the pin, or at least the
    # floor -- which is the proof it is the one the manager just wrote and not an
    # older or unrelated one elsewhere on PATH. If the manager cannot name its
    # destination, or the copy there does not answer at the required version, this
    # returns failure: the machine half is refused rather than run against a
    # guessed, possibly stale copy.
    directory=$(manager_bin_dir)
    [ -n "$directory" ] || return 1
    [ -x "$directory/agentic-hil" ] || return 1
    found_version=$("$directory/agentic-hil" --version 2>/dev/null) || return 1
    if version_matches_request "$found_version"; then
        printf '%s\n' "$directory"
        return 0
    fi
    return 1
}

report_path() {
    if [ "$NEEDS_PACKAGE" -eq 0 ]; then
        # Nothing was installed: the copy step 1 probed and accepted on this PATH
        # is the one the machine half uses, and it already resolves here.
        step 3 "PATH: agentic-hil ${installed:-} is already here and was kept, nothing to add"
        return 0
    fi
    if found_dir=$(installed_executable_dir); then
        # Call this exact copy for the machine half, and put its directory first
        # for the rest of the run: it, not an older agentic-hil earlier on PATH,
        # is what registers the skill and the MCP server.
        AGENTIC_HIL_CMD="$found_dir/agentic-hil"
        case ":$PATH:" in
            *":$found_dir:"*)
                step 3 "PATH: agentic-hil is installed in $found_dir, already on your PATH"
                ;;
            *)
                step 3 "PATH: agentic-hil landed in $found_dir, which is not on your PATH"
                say "PATH: add this line to your shell profile yourself, then open a new shell:"
                printf '\n    export PATH="%s:$PATH"\n\n' "$found_dir"
                ;;
        esac
        PATH="$found_dir:$PATH"
        export PATH
        return 0
    fi
    # Installed, but the fresh copy could not be located and version-checked where
    # the manager put it. We do not fall back to whatever bare agentic-hil PATH
    # resolves: that is exactly the older copy earlier on PATH this step exists to
    # step past. AGENTIC_HIL_CMD stays the bare name, and step 4 refuses to run the
    # machine half against it rather than register with a copy this run cannot
    # prove is the one it installed.
    step 3 "PATH: agentic-hil is installed but the fresh copy does not resolve here; TROUBLESHOOTING.md section 1 has the fix"
    return 0
}

detect_agents() {
    for cli in claude codex opencode; do
        if have "$cli"; then
            case "$cli" in
                claude) printf '%s\n' "claude-code" ;;
                *) printf '%s\n' "$cli" ;;
            esac
        fi
    done
}

process_name_for() {
    case "$1" in
        claude-code | claude) printf '%s' "claude" ;;
        *) printf '%s' "$1" ;;
    esac
}

running_pid() {
    if have pgrep; then
        pgrep -x "$1" 2>/dev/null | head -n 1
    fi
}

DISCOVERED_PYTHON=""

# Step 1: is a new enough agentic-hil already here?
NEEDS_PACKAGE=1
if ! have agentic-hil; then
    step 1 "probe: no agentic-hil on this PATH, installing it user-local"
elif ! installed=$(agentic-hil --version 2>/dev/null); then
    step 1 "probe: an agentic-hil on this PATH does not answer, installing it again"
elif [ -n "$PINNED" ]; then
    step 1 "probe: agentic-hil $installed is here, and --version $PINNED was asked for, so the package is installed again"
elif version_at_least "$installed" "$RELEASE"; then
    step 1 "probe: agentic-hil $installed is here and not older than $RELEASE, skipping the package install"
    NEEDS_PACKAGE=0
else
    step 1 "probe: agentic-hil $installed is older than $RELEASE, upgrading it"
fi

# Step 2: install the package, user-local, never as root.
if [ "$NEEDS_PACKAGE" -eq 0 ]; then
    step 2 "package: nothing to install"
elif have uv; then
    step 2 "package: uv is here, installing $(package_spec) user-local with uv tool install"
    install_with_uv
    PACKAGE_MANAGER="uv"
elif DISCOVERED_PYTHON=$(find_python); then
    step 2 "package: installing $(package_spec) user-local with $DISCOVERED_PYTHON -m pip install --user"
    pip_status=0
    install_with_pip "$DISCOVERED_PYTHON" || pip_status=$?
    if [ "$pip_status" -eq 3 ]; then
        say "package: this Python is externally managed (PEP 668), so pip cannot own it; falling back to uv"
        if ! have uv; then
            fetch_uv || fail "package: no uv here and no way to fetch it; install uv or pipx by hand, then run this again"
            user_bin_on_path
        fi
        have uv || fail "package: uv installed but does not resolve yet; open a new shell and run this again"
        install_with_uv
        PACKAGE_MANAGER="uv"
    elif [ "$pip_status" -ne 0 ]; then
        fail "package: pip could not install $(package_spec); TROUBLESHOOTING.md section 1 has the fallbacks"
    else
        PACKAGE_MANAGER="pip"
    fi
else
    step 2 "package: no uv and no Python 3.10 or newer here, fetching Astral's uv installer first"
    fetch_uv || fail "package: could not fetch the uv installer; install uv or Python 3.10 or newer by hand, then run this again"
    user_bin_on_path
    have uv || fail "package: uv installed but does not resolve yet; open a new shell and run this again"
    install_with_uv
    PACKAGE_MANAGER="uv"
fi

# Step 3: say where it landed, and edit nobody's shell profile.
report_path

# Refuse to run the machine half against a copy this run could not prove is the
# one it installed. AGENTIC_HIL_CMD is still the bare name only when a package
# was installed and step 3 could not resolve the fresh copy; running the bare
# name there would hand agent-install to whatever older agentic-hil PATH resolves,
# which is the failure this whole path guards against.
ensure_resolved_for_agent_install() {
    if [ "$NEEDS_PACKAGE" -eq 1 ] && [ "$AGENTIC_HIL_CMD" = "agentic-hil" ]; then
        fail "agent: the agentic-hil this run installed could not be located where the package manager put it, so the machine half was not run against a possibly stale PATH copy; TROUBLESHOOTING.md section 1 has the fix"
    fi
}

# One agent registered, reported as a result rather than as a document.
# `agent-install` answers with a JSON report of every path it touched, and on a
# machine with three agent CLIs that is roughly a hundred and fifty lines of
# machine-readable detail inside a transcript whose whole job is to say five calm
# things. On success the operator needs one line. On failure the document is the
# diagnosis, so then it is printed whole and this stops. The top-level "ok" is
# read at its own indentation, so a true nested inside a failed report cannot
# stand in for the answer.
# One agent registered, reported as a result rather than as a document. On
# success the operator needs one line; on failure the report is the diagnosis
# and is printed whole, which is why it is captured rather than streamed: three
# agent CLIs would otherwise put a hundred and fifty lines of detail inside a
# transcript whose whole job is to say five calm things.
#
# The verdict is the exit status, which is `overall_success` computed inside the
# CLI. It used to be that status and a match on the top-level `ok` at its own
# indentation, from back when the report arrived as JSON whoever was reading. It
# is prose now, addressed to the operator who is about to read it, and a text
# match on prose would be a worse check than the status it was doubling.
register_agent() {
    registering_agent="$1"
    if agent_install_report=$("$AGENTIC_HIL_CMD" agent-install --agent "$registering_agent" 2>&1); then
        say "agent: $registering_agent registered (skill and MCP server, restart pending)"
        return 0
    fi
    printf '%s\n' "$agent_install_report" >&2
    fail "agent: agent-install failed for $registering_agent; the report above says which half"
}

# Step 4: the machine half, and only the machine half.
CONFIGURED=""
if [ "$WITH_AGENT_INSTALL" -eq 0 ]; then
    step 4 "agent: --no-agent-install was given, so nothing of any agent's was written"
elif [ -n "$AGENT" ]; then
    ensure_resolved_for_agent_install
    step 4 "agent: registering the skill and the MCP server for $AGENT"
    register_agent "$AGENT"
    CONFIGURED="$AGENT"
else
    DETECTED=$(detect_agents)
    if [ -z "$DETECTED" ]; then
        step 4 "agent: no claude, codex or opencode CLI on this PATH, so nothing of an agent's was written"
        say "agent: the package is installed. Once your agent CLI is here, run this one line:"
        printf '\n    agentic-hil agent-install --agent <claude-code|codex|opencode>\n\n'
    else
        ensure_resolved_for_agent_install
        for agent_id in $DETECTED; do
            step 4 "agent: registering the skill and the MCP server for $agent_id"
            register_agent "$agent_id"
            CONFIGURED="$CONFIGURED $agent_id"
        done
    fi
fi

# Step 5: the one thing that stays the operator's. Every running agent CLI is
# named, not the first one found: an operator with two of them open restarts
# both, and a warning that names one is read as clearing the other.
RUNNING_LIST=""
RUNNING_COUNT=0
RUNNING_NAME=""
RUNNING_PID=""
for agent_id in $CONFIGURED; do
    process=$(process_name_for "$agent_id")
    pid=$(running_pid "$process")
    if [ -n "$pid" ]; then
        RUNNING_LIST="${RUNNING_LIST}    ${process} (PID ${pid})\n"
        RUNNING_COUNT=$((RUNNING_COUNT + 1))
        RUNNING_NAME="$process"
        RUNNING_PID="$pid"
    fi
done
if [ "$RUNNING_COUNT" -eq 0 ] && [ -n "${CLAUDECODE:-}" ]; then
    # Running inside a Claude Code session, which is the only signal where there
    # is no pgrep. It speaks for claude-code alone: an operator who asked for
    # codex is not told to restart something else.
    for agent_id in $CONFIGURED; do
        case "$agent_id" in
            claude-code | claude)
                RUNNING_NAME="claude"
                RUNNING_PID="this session"
                RUNNING_COUNT=1
                ;;
        esac
    done
fi

if [ "$RUNNING_COUNT" -eq 1 ]; then
    step 5 "restart: $RUNNING_NAME is running right now, and that is the one step left"
    printf '\n'
    printf '========================================================================\n'
    printf '  RESTART REQUIRED: %s is running right now (PID %s).\n' "$RUNNING_NAME" "$RUNNING_PID"
    printf '  Quit that process and start it again once. An agent CLI reads its MCP\n'
    printf '  registrations when a session starts, so the agentic-hil tools appear in\n'
    printf '  the next session, not in this one.\n'
    printf '========================================================================\n'
    printf '\n'
elif [ "$RUNNING_COUNT" -gt 1 ]; then
    step 5 "restart: $RUNNING_COUNT of your agent CLIs are running right now, and that is the one step left"
    printf '\n'
    printf '========================================================================\n'
    printf '  RESTART REQUIRED: these agent CLIs are running right now:\n'
    printf '%b' "$RUNNING_LIST"
    printf '  Quit each process and start it again once. An agent CLI reads its MCP\n'
    printf '  registrations when a session starts, so the agentic-hil tools appear in\n'
    printf '  the next session, not in this one.\n'
    printf '========================================================================\n'
    printf '\n'
else
    step 5 "restart: no agent CLI of yours is running, so there is nothing to restart"
    say "The next start of your agent has everything, and the first hardware question creates this project's configuration."
fi
