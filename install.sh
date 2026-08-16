#!/bin/sh
# What this script touches, and nothing else:
#   1. the agentic-hil package, installed user-local (uv tool, or pip --user).
#   2. your agent's skill file, under your own home directory.
#   3. your agent's user-level MCP registration, under your own home directory.
#   4. nothing in any repository, no project configuration, no shell rc file.
#   5. nothing that needs administrator rights: no sudo, no system package manager.

set -eu

FLOOR="0.4.0"

AGENT=""
WITH_AGENT_INSTALL=1
PINNED=""
WITH_CAN=1
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
       curl -LsSf https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.sh | sh
       curl -LsSf https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.sh | sh -s -- --agent claude

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

# The lowest version a freshly installed copy may report and still be accepted as
# the one this run installed: the pin when one was asked for, the floor otherwise.
# A copy older than this in a candidate directory is a stale one left behind, not
# what the manager just wrote, so it does not get to answer for the install.
required_version() {
    if [ -n "$PINNED" ]; then
        printf '%s' "$PINNED"
    else
        printf '%s' "$FLOOR"
    fi
}

fetch_uv() {
    # Astral's own installer for uv. It downloads the release archive for this
    # platform and verifies its published checksums itself, which is why nothing
    # here adds a second, weaker check of its own around the pipe.
    if have curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif have wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        return 1
    fi
}

user_bin_on_path() {
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    export PATH
}

install_with_uv() {
    uv tool install --upgrade "$(package_spec)"
}

install_with_pip() {
    python_bin="$1"
    if pip_output=$("$python_bin" -m pip install --user --upgrade "$(package_spec)" 2>&1); then
        printf '%s\n' "$pip_output"
        return 0
    fi
    printf '%s\n' "$pip_output" >&2
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
    # uv's own executable directory, straight from uv. This is authoritative: it
    # honours UV_TOOL_BIN_DIR and the XDG data directory, neither of which the
    # guessed user bins below can name, and it is where `uv tool install` just
    # wrote the console script. pip --user writes its scripts into the
    # interpreter's user scripts path, which candidate_bin_dirs already probes,
    # so only the uv branch needs naming here.
    if [ "${PACKAGE_MANAGER:-}" = "uv" ]; then
        uv tool dir --bin 2>/dev/null || true
    fi
}

candidate_bin_dirs() {
    # The manager's own executable directory first, so an install whose copy
    # landed outside the default user bin is still found there; the guessed user
    # bins follow as a fallback for a uv too old to report its bin directory.
    manager_bin_dir
    printf '%s\n' "${XDG_BIN_HOME:-$HOME/.local/bin}"
    printf '%s\n' "$HOME/.local/bin"
    if [ -n "${DISCOVERED_PYTHON:-}" ]; then
        "$DISCOVERED_PYTHON" -c 'import sysconfig; print(sysconfig.get_path("scripts", "posix_user"))' 2>/dev/null || true
    fi
}

# The exact agentic-hil the machine half calls. It stays the bare name only when
# step 1 found a new-enough copy already here and installed nothing; once this
# run installs a copy, it becomes that copy's own path, so an older agentic-hil
# earlier on PATH cannot answer for the install that just happened.
AGENTIC_HIL_CMD="agentic-hil"

installed_executable_dir() {
    # The first candidate bin directory that now holds an agentic-hil which
    # answers and is at least the version this run asked for. The version check is
    # the proof that this is the copy the manager just wrote and not an older one
    # left in a candidate directory; the manager's own bin directory is tried
    # first, so a copy that landed outside the default user bin is still found.
    minimum=$(required_version)
    while IFS= read -r directory; do
        [ -n "$directory" ] || continue
        [ -x "$directory/agentic-hil" ] || continue
        found_version=$("$directory/agentic-hil" --version 2>/dev/null) || continue
        if version_at_least "$found_version" "$minimum"; then
            printf '%s\n' "$directory"
            return 0
        fi
    done <<EOF
$(candidate_bin_dirs)
EOF
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
elif version_at_least "$installed" "$FLOOR"; then
    step 1 "probe: agentic-hil $installed is here and at least $FLOOR, skipping the package install"
    NEEDS_PACKAGE=0
else
    step 1 "probe: agentic-hil $installed is older than $FLOOR, upgrading it"
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

# Step 4: the machine half, and only the machine half.
CONFIGURED=""
if [ "$WITH_AGENT_INSTALL" -eq 0 ]; then
    step 4 "agent: --no-agent-install was given, so nothing of any agent's was written"
elif [ -n "$AGENT" ]; then
    ensure_resolved_for_agent_install
    step 4 "agent: registering the skill and the MCP server for $AGENT"
    "$AGENTIC_HIL_CMD" agent-install --agent "$AGENT"
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
            "$AGENTIC_HIL_CMD" agent-install --agent "$agent_id"
            CONFIGURED="$CONFIGURED $agent_id"
        done
    fi
fi

# Step 5: the one thing that stays the operator's.
RUNNING_NAME=""
RUNNING_PID=""
for agent_id in $CONFIGURED; do
    process=$(process_name_for "$agent_id")
    pid=$(running_pid "$process")
    if [ -n "$pid" ] && [ -z "$RUNNING_NAME" ]; then
        RUNNING_NAME="$process"
        RUNNING_PID="$pid"
    fi
done
if [ -z "$RUNNING_NAME" ] && [ -n "${CLAUDECODE:-}" ]; then
    # Running inside a Claude Code session, which is the only signal where there
    # is no pgrep. It speaks for claude-code alone: an operator who asked for
    # codex is not told to restart something else.
    for agent_id in $CONFIGURED; do
        case "$agent_id" in
            claude-code | claude)
                RUNNING_NAME="claude"
                RUNNING_PID="this session"
                ;;
        esac
    done
fi

if [ -n "$RUNNING_NAME" ]; then
    step 5 "restart: $RUNNING_NAME is running right now, and that is the one step left"
    printf '\n'
    printf '========================================================================\n'
    printf '  RESTART REQUIRED: %s is running right now (PID %s).\n' "$RUNNING_NAME" "$RUNNING_PID"
    printf '  Quit that process and start it again once. An agent CLI reads its MCP\n'
    printf '  registrations when a session starts, so the agentic-hil tools appear in\n'
    printf '  the next session, not in this one.\n'
    printf '========================================================================\n'
    printf '\n'
else
    step 5 "restart: no agent CLI of yours is running, so there is nothing to restart"
    say "The next start of your agent has everything, and the first hardware question creates this project's configuration."
fi
