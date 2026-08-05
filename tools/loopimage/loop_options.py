"""The rules both halves of tools/loop_in_container.py enforce.

The wrapper refuses on the host, before a model is spent; the entrypoint refuses
again inside the container, because a value that reached the loop by another
route is still a value the loop would obey. Two enforcement points are the
point. Two copies of the *rule* were not: the copies grew apart, and the gap was
the way through -- a check that read the outer option name and stopped there let
`--codex-arg=--cd=/repo` past, which puts the reviewer back in the read-write
mount the container exists to keep it out of.

Nothing here touches the filesystem or the environment, so either half can ask
these questions wherever it runs.
"""

from __future__ import annotations

# Inner options that decide what the container isolates rather than how the loop
# is run. The wrapper sets all four itself: a second --codex-sandbox is how the
# value that was measured stops being the value that runs, and a
# --review-checkout-dir or --review-checkout none puts the reviewer's test runs
# back onto the repository mount, which is the Windows failure this buries.
CONTAINER_OWNED_OPTIONS = ("--repo", "--codex-sandbox", "--review-checkout", "--review-checkout-dir")

# Inner options naming where the run's paperwork lands. They are forwarded, and
# they are checked: a directory outside the mount dies with the container, and
# then nothing the wrapper printed about it was ever true.
PAPERWORK_OPTIONS = ("--review-dir", "--log-dir")

# --codex-arg is forwarded, because it is how a caller reaches a Codex flag this
# loop does not model. Its payload is read rather than waved through: the loop
# appends it to the Codex command, and Codex takes the last occurrence.
CODEX_ARGUMENT_OPTION = "--codex-arg"
# Codex flags that move the reviewer's working root or replace the sandbox the
# container settled on. --cd is the one that matters: pointed at the mount, the
# reviewer's test runs, caches and temporary directories land in the tree the
# implementer is editing -- both agents in one working tree again, on the
# filesystem whose permissions this container exists to get away from.
ISOLATING_CODEX_OPTIONS = ("-C", "--cd", "-s", "--sandbox", "--dangerously-bypass-approvals-and-sandbox")
# `-c key=value` reaches the same two settings through Codex's configuration.
CODEX_CONFIGURATION_OPTIONS = ("-c", "--config")
# Codex's parser also takes a short option with its value attached, so `-C/repo`
# and `-csandbox_mode=x` are the same arguments as their spaced spellings.
SHORT_CODEX_OPTIONS = ("-C", "-s", "-c")


def expansions(name: str, options: tuple[str, ...]) -> list[str]:
    """Which of `options` a forwarded name can reach.

    The inner loop's parser accepts any unambiguous prefix of an option, so a
    rule that reads `--codex-sandbox` and lets `--codex-sand` past reads nothing
    at all: both arrive at the same destination.
    """
    if not name.startswith("--"):
        return []
    return [option for option in options if option.startswith(name)]


def option_values(forwarded: list[str], options: tuple[str, ...]) -> list[tuple[str, str]]:
    """Each (option, value) pair in `forwarded`, in the order it was given.

    Both spellings are read, `--name=value` and `--name value`, because the inner
    parser reads both.
    """
    pairs: list[tuple[str, str]] = []
    for index, argument in enumerate(forwarded):
        name, separator, attached = argument.partition("=")
        reached = expansions(name, options)
        if len(reached) != 1:
            # No match, or an abbreviation the inner parser will reject as
            # ambiguous; either way there is nothing here to read.
            continue
        if separator:
            pairs.append((reached[0], attached))
        elif index + 1 < len(forwarded):
            pairs.append((reached[0], forwarded[index + 1]))
    return pairs


def owned_options(forwarded: list[str]) -> list[str]:
    """The forwarded options that decide what the container isolates."""
    return sorted(
        {name for argument in forwarded if expansions(name := argument.split("=", 1)[0], CONTAINER_OWNED_OPTIONS)}
    )


def paperwork_values(forwarded: list[str]) -> dict[str, str]:
    """The value each paperwork option was given; the last occurrence wins, as argparse does."""
    return dict(option_values(forwarded, PAPERWORK_OPTIONS))


def isolating_configuration(setting: str) -> bool:
    """Whether a Codex `key=value` override decides the sandbox or the working root."""
    key = setting.split("=", 1)[0].strip()
    return key == "cwd" or key.startswith("sandbox")


def isolating_codex_arguments(forwarded: list[str]) -> list[str]:
    """The forwarded Codex arguments that would move the reviewer or its sandbox.

    Every spelling Codex accepts is covered, because Codex accepts every
    spelling: `--cd=/repo`, `--cd /repo`, `-C/repo`, `-C /repo`, and the same
    settings reached as `-c sandbox_mode=...` in either of its two forms.
    """
    named: list[str] = []
    configuration_expected = False
    for payload in [value for _option, value in option_values(forwarded, (CODEX_ARGUMENT_OPTION,))]:
        if configuration_expected:
            configuration_expected = False
            if isolating_configuration(payload):
                named.append(payload)
            continue
        name, separator, attached = payload.partition("=")
        short = next(
            (option for option in SHORT_CODEX_OPTIONS if payload.startswith(option) and len(payload) > len(option)),
            None,
        )
        if short is not None and not payload.startswith("--"):
            name, separator, attached = short, "=", payload[len(short) :].lstrip("=")
        if name in CODEX_CONFIGURATION_OPTIONS:
            if not separator:
                # The setting is the next --codex-arg the caller passed.
                configuration_expected = True
            elif isolating_configuration(attached):
                named.append(payload)
        elif name in ISOLATING_CODEX_OPTIONS:
            named.append(name)
    return sorted(set(named))
