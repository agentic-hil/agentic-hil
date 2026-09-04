#!/bin/sh
# What this script touches, and nothing else:
#   1. the agentic-hil package, installed user-local (uv tool, or pip --user).
#   2. your agent's skill file, under your own home directory.
#   3. your agent's user-level MCP registration, under your own home directory.
#   4. nothing in any repository, no project configuration, no shell rc file.
#   5. nothing that needs administrator rights: no sudo, no system package manager.

set -eu

# The release this script installs, and the number step 1 compares an
# installation already here against. It decides what the transcript calls the
# run, not whether the run happens: this line is the emergency anchor people
# re-run when `agentic-hil upgrade` itself is what broke, so an existing
# installation always reaches step 2's manager invocation, which reinstalls it in
# place rather than resolving it as already-current and leaving a broken copy
# untouched. Deliberately not a capability floor either: step 4 registers the
# skill out of whatever copy step 1 left in place, so a floor left a returning
# user on an old package and an old skill at once.
RELEASE="0.21.2"

# The PATH this run was handed, recorded before anything of ours has prepended to
# it. Step 3's report is about the operator's own shell, and this script edits
# PATH for its own use twice on the way there: `user_bin_on_path` puts the user
# bin in front so a uv it has just fetched resolves for the rest of the run. Step
# 3 then compared against that edited value, so on the commonest Linux of all,
# where an externally managed python3 sends the run through uv, it told a reader
# whose shell has no user bin on PATH that the directory was already on it, and
# withheld the export line they needed (#430). The report reads this snapshot
# instead, so it answers for the shell the reader is standing in.
STARTUP_PATH="$PATH"

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

# A development tree: an editable checkout of this repository, which reports
# X.Y.Z.devN. It is the one installation already here that this script keeps
# instead of sending through the manager, whatever number it carries, because
# step 2 would replace the operator's own working copy with a release from PyPI
# (#291). The marker is a suffix and not a number, so it is read as one: the
# comparisons above stop at the digits of each field by design, which is what
# lets a development version compare as its X.Y.Z, and is also why neither of
# them can see this.
version_is_development() {
    case "$1" in
        *.dev | *.dev[0-9]*) return 0 ;;
        *) return 1 ;;
    esac
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

# Can this interpreter run pip at all, asked before an install is attempted with
# it rather than read out of the wreckage afterwards. Debian and Ubuntu ship
# python3 with no pip module unless python3-pip is installed, and so do most
# minimal container images, which makes a pip-less Python the default state of
# the most common Linux rather than a broken machine. There, `-m pip install`
# answers "No module named pip" and exits 1, which carries none of the words the
# PEP 668 branch reads, so before this question was asked the most ordinary
# Linux of all stopped at step 2 with "pip could not install" while the script
# already knew how to fetch uv. A pip that answers here and then fails at the
# install is a real pip failure and still fails: this decides only whether there
# is a pip to try.
python_has_pip() {
    "$1" -m pip --version >/dev/null 2>&1
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

# Does a freshly installed copy's reported version answer what this run asked for.
# A pin has to match exactly: the documented --version contract is an exact
# release, so a newer copy left in the manager's bin is not the release this run
# wrote. An unpinned run named no version at all, so there is nothing here to
# compare it against; the release decides one thing only, in step 1, where it is
# what that step calls the run, and what proves a copy at step 3 is where it sits
# (see installed_executable_dir).
version_matches_request() {
    if [ -n "$PINNED" ]; then
        version_exactly "$1" "$PINNED"
    else
        return 0
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
    # For the rest of this run only: a uv just fetched into the user bin has to
    # resolve for the steps that follow. Step 3 reports from STARTUP_PATH rather
    # than from this edit, so the edit never becomes a claim about the reader's
    # own shell.
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    export PATH
}

# One uv command, run with the certificate-retry branches around it, because the
# text of a failure is what decides whether there is a second attempt to make.
# The command's own words are this function's positional parameters, so the
# refresh path (`tool upgrade --reinstall`) and the fresh and pin paths (`tool
# install ...`) share one retry. uv's output is captured rather than streamed for
# the same reason: the retry reads it.
run_uv() {
    if uv_output=$(uv "$@" 2>&1); then
        printf '%s\n' "$uv_output"
        return 0
    fi
    printf '%s\n' "$uv_output" >&2
    if [ "$SYSTEM_CERTS" = "auto" ] && trust_failure "$uv_output"; then
        say "certificates: that is a certificate uv cannot get to a root it carries, which is what a TLS-intercepting proxy looks like from inside uv; retrying once against this machine's own store, with verification still on"
        use_system_certs
        SYSTEM_CERTS="always"
        if uv_output=$(uv "$@" 2>&1); then
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

# Whether uv already owns this tool. A tool uv installed is one `uv tool upgrade`
# can rebuild from the requirement uv itself recorded, extras and pin included; a
# copy pip put on PATH is not one, and asking uv to upgrade it would only fail.
# The probe keeps a refresh on the command that preserves what the tool was
# installed with, and falls back to a plain install only when there is no uv tool
# for uv to upgrade.
uv_manages_tool() {
    uv tool list 2>/dev/null | grep -q '^agentic-hil[[:space:]]'
}

# The requirement set uv recorded for the tool it owns, printed only when uv keeps
# a receipt this can read in full AND the whole recorded install can be replayed
# faithfully; a non-zero return says the reconstruction would change what uv
# recorded, so the caller keeps to the upgrade that preserves it verbatim instead.
# That covers a missing or unreadable receipt, a receipt whose requirements array
# never opened or never closed, a recorded tool option (an explicit `[tool.options]`
# index the reconstruction would drop), a root carrying a pin or a git/path/url
# source rather than a plain name and extras, and any `--with` requirement that
# does not rebuild to a bare `name[extras]specifier`.
#
# uv writes `agentic-hil[can,pyocd] --with requests==2.32.5` into a receipt beside
# the tool environment as a TOML array of requirement objects, inline for a lone
# root requirement and one-per-line the moment a `--with` is added:
#
#     requirements = [
#         { name = "agentic-hil", extras = ["can", "pyocd"] },
#         { name = "requests", specifier = "==2.32.5" },
#     ]
#
# The output is the agentic-hil root's extras on the first line (space-separated,
# possibly empty) and every other recorded requirement on a following line, rebuilt
# as a `--with` PEP 508 string (`name[extras]specifier`). Reading the whole set
# back is what lets a refresh add the extra THIS run asks for while keeping both a
# root extra an earlier install recorded and a `--with` requirement it recorded;
# reinstalling from the root alone drops the latter, which the previous reader did
# whenever the receipt went multiline. The parse spans lines and tracks bracket
# depth so the nested `extras = [...]` and the trailing `entrypoints = [...]` block
# do not confuse where the requirement list ends.
uv_recorded_requirements() {
    uv_tool_root=$(uv tool dir 2>/dev/null) || return 1
    uv_receipt="${uv_tool_root%/}/agentic-hil/uv-receipt.toml"
    [ -r "$uv_receipt" ] || return 1
    awk '
    function extras_of(obj,   ex, out) {
        out = ""
        if (match(obj, /extras = \[[^]]*\]/)) {
            ex = substr(obj, RSTART, RLENGTH)
            while (match(ex, /"[^"]*"/)) {
                out = out (out == "" ? "" : " ") substr(ex, RSTART + 1, RLENGTH - 2)
                ex = substr(ex, RSTART + RLENGTH)
            }
        }
        return out
    }
    function object_ok(obj,   tmp, k) {
        # Only name/extras/specifier can be rebuilt as a bare PEP 508 string; a
        # marker, url or git/path field cannot, so refuse the whole receipt.
        tmp = obj
        while (match(tmp, /[A-Za-z][A-Za-z_-]*[ ]*=/)) {
            k = substr(tmp, RSTART, RLENGTH)
            sub(/[ ]*=$/, "", k)
            if (k != "name" && k != "extras" && k != "specifier") return 0
            tmp = substr(tmp, RSTART + RLENGTH)
        }
        return 1
    }
    function root_ok(obj,   tmp, k) {
        # The root requirement is rebuilt from its extras plus the pin this run
        # names, so only name and extras replay faithfully. A recorded specifier
        # (a pin), a directory/url/git source or a marker would be dropped by the
        # reconstruction, so refuse the whole receipt and let the caller keep to
        # the upgrade that preserves whatever uv recorded.
        tmp = obj
        while (match(tmp, /[A-Za-z][A-Za-z_-]*[ ]*=/)) {
            k = substr(tmp, RSTART, RLENGTH)
            sub(/[ ]*=$/, "", k)
            if (k != "name" && k != "extras") return 0
            tmp = substr(tmp, RSTART + RLENGTH)
        }
        return 1
    }
    function pep508(obj,   nm, out, ex, item, spec) {
        if (!match(obj, /name = "[^"]*"/)) return ""
        nm = substr(obj, RSTART, RLENGTH); gsub(/name = "|"/, "", nm)
        out = nm
        if (match(obj, /extras = \[[^]]*\]/)) {
            ex = substr(obj, RSTART, RLENGTH); item = ""
            while (match(ex, /"[^"]*"/)) {
                item = item (item == "" ? "" : ",") substr(ex, RSTART + 1, RLENGTH - 2)
                ex = substr(ex, RSTART + RLENGTH)
            }
            if (item != "") out = out "[" item "]"
        }
        if (match(obj, /specifier = "[^"]*"/)) {
            spec = substr(obj, RSTART, RLENGTH); gsub(/specifier = "|"/, "", spec)
            out = out spec
        }
        return out
    }
    function process(txt,   rest, obj, nm, root_seen, p) {
        # Fills the globals out_extras/out_withs and returns 1 on a receipt this
        # can replay in full, 0 on one it cannot; the caller prints only after a
        # 1, so a rejected receipt reaches the preserving upgrade instead of a
        # reconstruction from a partial read.
        out_withs = ""; root_seen = 0; out_extras = ""
        rest = txt
        while (match(rest, /\{[^{}]*\}/)) {
            obj = substr(rest, RSTART, RLENGTH)
            rest = substr(rest, RSTART + RLENGTH)
            nm = ""
            if (match(obj, /name = "[^"]*"/)) { nm = substr(obj, RSTART, RLENGTH); gsub(/name = "|"/, "", nm) }
            if (nm == "agentic-hil" && !root_seen) {
                if (!root_ok(obj)) return 0
                root_seen = 1
                out_extras = extras_of(obj)
            } else {
                if (!object_ok(obj)) return 0
                p = pep508(obj)
                if (p == "" || index(p, " ") > 0) return 0
                out_withs = out_withs p "\n"
            }
        }
        if (!root_seen) return 0
        return 1
    }
    BEGIN { state = 0; options = 0; in_options = 0 }
    {
        line = $0
        # A recorded tool option (an explicit index, a python pin) cannot be
        # replayed by the reconstruction, which passes none, so a receipt that
        # records any is refused and the caller keeps to the preserving upgrade.
        # uv writes these under a `[tool.options]` table (or sub-table / array of
        # tables) only when there are some, so a bare header with no key before the
        # next section is treated as no options.
        if (line ~ /^[ \t]*\[\[?tool\.options/) {
            if (line ~ /^[ \t]*\[tool\.options\][ \t]*$/) { in_options = 1 }
            else { options = 1; in_options = 0 }
            next
        }
        if (in_options) {
            if (line ~ /^[ \t]*\[/) { in_options = 0 }
            else if (line ~ /[^ \t]/ && line !~ /^[ \t]*#/) { options = 1 }
        }
        if (state == 2) next
        s = line
        if (state == 0) {
            p = index(s, "requirements = [")
            if (p == 0) next
            s = substr(s, p + 16)
            state = 1; depth = 1
        }
        n = length(s)
        for (i = 1; i <= n; i++) {
            c = substr(s, i, 1)
            if (c == "]") { depth--; if (depth == 0) { state = 2; break }; interior = interior c }
            else if (c == "[") { depth++; interior = interior c }
            else interior = interior c
        }
        if (state == 1) interior = interior "\n"
    }
    # The receipt is read whole before anything is printed: the requirements array
    # must have opened AND closed (state 2, a missing anchor leaves 0, a truncated
    # array 1), no tool option may be recorded, and every requirement must replay.
    END {
        if (state != 2) exit 1
        if (options) exit 1
        if (!process(interior)) exit 1
        print out_extras
        printf "%s", out_withs
        exit 0
    }
    ' "$uv_receipt"
}

# The `--with` arguments a uv-managed refresh replays, built from the recorded
# requirement set ($1, the multi-line value uv_recorded_requirements printed): its
# second line onward, each turned into `--with <req>`. The reconstructed
# requirements are bare PEP 508 strings with no spaces, so the caller can rely on
# word-splitting to pass them as separate arguments.
uv_with_flags() {
    printf '%s\n' "$1" | sed -n '2,$p' | while IFS= read -r recorded_with; do
        [ -n "$recorded_with" ] && printf -- '--with %s ' "$recorded_with"
    done
}

# The requirement a uv-managed refresh reinstalls from: this run's extras merged
# with the ones uv already recorded (passed as $1, whitespace-separated and
# possibly empty), spelled agentic-hil[...] with this run's pin if there is one.
# Merging closes both gaps at once, the recorded pyocd an earlier install left
# survives (a bare `tool install agentic-hil[can]` would drop it), and the `can` a
# --can run wants is added even when the recorded requirement was bare (a bare
# `tool upgrade` would never add it).
refresh_spec() {
    refresh_extras=""
    if [ "$WITH_CAN" -eq 1 ]; then
        refresh_extras="can"
    fi
    for recorded_extra in $1; do
        case " $refresh_extras " in
            *" $recorded_extra "*) ;;
            *) refresh_extras="${refresh_extras:+$refresh_extras }$recorded_extra" ;;
        esac
    done
    refresh_result="agentic-hil"
    if [ -n "$refresh_extras" ]; then
        refresh_result="agentic-hil[$(printf '%s' "$refresh_extras" | tr ' ' ',')]"
    fi
    if [ -n "$PINNED" ]; then
        refresh_result="${refresh_result}==${PINNED}"
    fi
    printf '%s' "$refresh_result"
}

install_with_uv() {
    case "$INSTALL_MODE" in
        refresh)
            if uv_manages_tool; then
                # uv owns this tool. Reinstall from the requirement uv recorded
                # merged with this run's extras: the recorded `[can,pyocd]`
                # survives (a `tool install agentic-hil[can]` would drop pyocd)
                # and a `--can` a bare recorded requirement never had is added (a
                # `tool upgrade` would never add it). --reinstall replaces the
                # files even when the recorded version is already current, which is
                # the repair the anchor exists for. A recorded `--with` requirement
                # is replayed as its own --with so the reinstall keeps it too; a bare
                # `tool install agentic-hil[...]` would drop it. When uv keeps no
                # readable receipt, or records a requirement this cannot rebuild
                # without changing it, fall back to the upgrade that preserves
                # whatever it did record rather than reinstalling from a set this
                # could not read back in full.
                if recorded=$(uv_recorded_requirements); then
                    recorded_extras=$(printf '%s\n' "$recorded" | sed -n '1p')
                    with_flags=$(uv_with_flags "$recorded")
                    # Word-splitting on with_flags is intended: each replayed
                    # requirement is a space-free PEP 508 string.
                    # shellcheck disable=SC2086
                    run_uv tool install --upgrade --reinstall "$(refresh_spec "$recorded_extras")" $with_flags
                else
                    run_uv tool upgrade --reinstall agentic-hil
                fi
                return 0
            fi
            run_uv tool install --upgrade --reinstall "$(package_spec)"
            ;;
        pin)
            # A named release sets the requirement outright, so it goes through
            # install; --reinstall still forces the replacement even when the
            # installed version already equals the pin.
            run_uv tool install --upgrade --reinstall "$(package_spec)"
            ;;
        *)
            run_uv tool install --upgrade "$(package_spec)"
            ;;
    esac
}

install_with_pip() {
    python_bin="$1"
    # pip leaves a package whose installed version already satisfies the request
    # untouched under --upgrade, so a refresh or a pin adds --force-reinstall to
    # make it replace the files; a first install has nothing to reinstall. pip has
    # no recorded requirement of its own, so the extras a refresh keeps are the
    # ones already on disk, which --force-reinstall does not remove.
    pip_reinstall=""
    case "$INSTALL_MODE" in
        refresh | pin) pip_reinstall="--force-reinstall" ;;
    esac
    # shellcheck disable=SC2086 # pip_reinstall is one optional flag, split on purpose
    if pip_output=$("$python_bin" -m pip install --user --upgrade $pip_reinstall "$(package_spec)" 2>&1); then
        printf '%s\n' "$pip_output"
        return 0
    fi
    printf '%s\n' "$pip_output" >&2
    if [ "$SYSTEM_CERTS" = "auto" ] && trust_failure "$pip_output"; then
        use_system_certs
        SYSTEM_CERTS="always"
        if [ -n "${PIP_CERT:-}" ]; then
            say "certificates: retrying pip once against this machine's own store, with verification still on"
            # shellcheck disable=SC2086 # pip_reinstall is one optional flag, split on purpose
            if pip_output=$("$python_bin" -m pip install --user --upgrade $pip_reinstall "$(package_spec)" 2>&1); then
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

# The one uv takeover, for the two ways a discovered Python turns out not to be a
# route to an installation: it has no pip module at all, and it has one the
# distribution forbids pip to use (PEP 668). Both answers are the same, and both
# were arrived at after find_python had already succeeded, so the machine may
# have no uv either and this fetches it. The caller records PACKAGE_MANAGER, so
# step 3 asks uv rather than the interpreter where the executable landed.
uv_takes_over_from_pip() {
    if ! have uv; then
        fetch_uv || fail "package: no uv here and no way to fetch it; install uv or pipx by hand, then run this again"
        user_bin_on_path
    fi
    have uv || fail "package: uv installed but does not resolve yet; open a new shell and run this again"
    install_with_uv
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
# step 1 kept a development installation and this run installed nothing; once
# this run installs a copy, it becomes that copy's own path, so an older
# agentic-hil earlier on PATH cannot answer for the install that just happened.
AGENTIC_HIL_CMD="agentic-hil"

installed_executable_dir() {
    # The manager's own destination directory, and only that. Step 2 installed into
    # exactly this directory, so a copy that answers --version here is the copy the
    # manager just wrote and the newest requirement it resolved. The placement is
    # the proof: an older or unrelated agentic-hil elsewhere on PATH cannot get
    # into the directory the manager names.
    #
    # Requiring the release floor on top of that placement added no stale-copy
    # protection and cost the release window. Between the merge of a release commit
    # and the PyPI publish the index still serves the release below, so the script
    # demanded a version nobody could install yet and refused a wholly correct fresh
    # install as possibly stale (#310). The release keeps its one real job, in step
    # 1, where it decides whether a copy already here is called upgraded or
    # refreshed.
    #
    # A pin is the one case where a version still has to be checked here. There the
    # operator named a release, step 2 either installed exactly that or failed
    # outright, and a copy in the manager's bin reporting anything else is a
    # leftover rather than this run's work.
    #
    # If the manager cannot name its destination, or the copy there does not answer,
    # this returns failure: the machine half is refused rather than run against a
    # guessed, possibly stale copy.
    directory=$(manager_bin_dir)
    [ -n "$directory" ] || return 1
    [ -x "$directory/agentic-hil" ] || return 1
    found_version=$("$directory/agentic-hil" --version 2>/dev/null) || return 1
    [ -n "$found_version" ] || return 1
    if version_matches_request "$found_version"; then
        printf '%s\n' "$directory"
        return 0
    fi
    return 1
}

report_path() {
    if [ "$NEEDS_PACKAGE" -eq 0 ]; then
        # Nothing was installed: the development copy step 1 kept on this PATH is
        # the one the machine half uses, and it already resolves here.
        step 3 "PATH: agentic-hil ${installed:-} is already here and was kept, nothing to add"
        return 0
    fi
    if found_dir=$(installed_executable_dir); then
        # Call this exact copy for the machine half, and put its directory first
        # for the rest of the run: it, not an older agentic-hil earlier on PATH,
        # is what registers the skill and the MCP server.
        AGENTIC_HIL_CMD="$found_dir/agentic-hil"
        # STARTUP_PATH, never the live PATH: the live one may already carry this
        # directory because this run put it there, and saying "already on your
        # PATH" about our own edit is the claim #430 is about.
        case ":$STARTUP_PATH:" in
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

# Step 1: what is already here, and what does this run call itself.
#
# An installation found here goes through step 2 either way. This line is the
# rescue path for an installation that is broken, or whose own `agentic-hil
# upgrade` is what broke, and answering "nothing to install" to the person who
# just watched their upgrade fail left the anchor with nothing to anchor. The
# comparison against the release chooses the word, upgrading or refreshing, and
# a development tree is the one copy that is kept.
# INSTALL_MODE says what step 2 is asking the manager to do, which is not the
# same question as whether it runs. `fresh` is a first install, and its --upgrade
# has nothing to reinstall. `refresh` is a copy already here that this run
# replaces in place: whatever it reports, the manager is told to reinstall it,
# because neither uv nor pip touches a package whose installed version already
# satisfies the request unless it is told to, and a current-but-broken copy is
# exactly the one the anchor exists to repair. `pin` is `refresh` with the
# operator's own release named, which sets the requirement outright.
NEEDS_PACKAGE=1
INSTALL_MODE="fresh"
if ! have agentic-hil; then
    step 1 "probe: no agentic-hil on this PATH, installing it user-local"
elif ! installed=$(agentic-hil --version 2>/dev/null); then
    INSTALL_MODE="refresh"
    step 1 "probe: an agentic-hil on this PATH does not answer, installing it again"
elif [ -n "$PINNED" ]; then
    INSTALL_MODE="pin"
    step 1 "probe: agentic-hil $installed is here, and --version $PINNED was asked for, so the package is installed again"
elif version_is_development "$installed"; then
    step 1 "probe: agentic-hil $installed is a development version, so it is kept: installing over it would replace an editable checkout with a release from PyPI"
    NEEDS_PACKAGE=0
elif version_at_least "$installed" "$RELEASE"; then
    INSTALL_MODE="refresh"
    step 1 "probe: agentic-hil $installed is here and not older than $RELEASE, refreshing this current installation"
else
    INSTALL_MODE="refresh"
    step 1 "probe: agentic-hil $installed is older than $RELEASE, upgrading it"
fi

# Step 2: install the package, user-local, never as root.
if [ "$NEEDS_PACKAGE" -eq 0 ]; then
    step 2 "package: nothing to install, the development installation stays as it is"
elif have uv; then
    step 2 "package: uv is here, installing $(package_spec) user-local with uv tool install"
    install_with_uv
    PACKAGE_MANAGER="uv"
elif DISCOVERED_PYTHON=$(find_python); then
    if ! python_has_pip "$DISCOVERED_PYTHON"; then
        step 2 "package: $DISCOVERED_PYTHON has no pip module, so pip cannot install with it; falling back to uv"
        uv_takes_over_from_pip
        PACKAGE_MANAGER="uv"
    else
        step 2 "package: installing $(package_spec) user-local with $DISCOVERED_PYTHON -m pip install --user"
        pip_status=0
        install_with_pip "$DISCOVERED_PYTHON" || pip_status=$?
        if [ "$pip_status" -eq 3 ]; then
            say "package: this Python is externally managed (PEP 668), so pip cannot own it; falling back to uv"
            uv_takes_over_from_pip
            PACKAGE_MANAGER="uv"
        elif [ "$pip_status" -ne 0 ]; then
            fail "package: pip could not install $(package_spec); TROUBLESHOOTING.md section 1 has the fallbacks"
        else
            PACKAGE_MANAGER="pip"
        fi
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
