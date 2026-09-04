"""README.md and docs/safety-model.md state one permission default, and the code decides it.

Both pages said "deny-by-default permissions per device", which a generated
configuration is not: `generated_permissions` writes every switch true except the
`EXCLUSIVE_FLASH_PERMISSIONS` pair, so what a bench denies until somebody
declares it is a *device*, not a switch on one. Each page carried that claim in
its own words, which is the failure this file exists to stop: correcting one
left the other standing, and the page a reader is sent to for the depth is the
one that outlives a landing page's edits.

So the two statements are located structurally (the section heading, then the
statement under it) rather than by quoting them, and every fact they are held to
is read off the code: the permission names off `generated_permissions` and the
project and section lists beside it, the withheld pair off
`EXCLUSIVE_FLASH_PERMISSIONS`, the refusal an undeclared device gets off a real
`resolve_device` on a really generated file, and the two commands off the CLI
parser that has to accept them. A page that renames a permission, claims one a
generation withholds, or invents one fails here rather than at a reader's bench.

The one anchor that is prose is the direction an agent may move a permission in.
The behaviour behind it is asserted directly against `permission_widening` in the
same file, so the sentence stays only while it is still true.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_hil.cli import build_parser, init_config
from agentic_hil.config import (
    GENERATED_PROJECT_PERMISSIONS,
    GENERATED_SECTION_PERMISSIONS,
    GENERATED_WRITE_PERMISSIONS,
    AgenticHILConfig,
    generated_permissions,
    load_authoritative_config,
    permission_summary,
)
from agentic_hil.configwrite import permission_widening
from agentic_hil.devices import DeviceError, resolve_device
from agentic_hil.knowledge import EXCLUSIVE_FLASH_PERMISSIONS

REPO_ROOT = Path(__file__).resolve().parent.parent

# A permission as a reader meets it in either document: backticked, which is how
# both pages spell every one of them and how a name in prose ("permission",
# "permission_denied") stays out of this.
PERMISSION_NAME = re.compile(r"`(allow_[a-z_]+)`")

# The pair a generation holds false, spelled from the constant rather than from
# the two names. A third flag joining the interlock, or either being renamed,
# leaves both pages claiming something the bench no longer does, and that is the
# day this has to fail.
PAIR_CLAUSE = " and ".join(f"`{flag}`" for flag in EXCLUSIVE_FLASH_PERMISSIONS)

# The MCP asymmetry, in the words both pages use for it. Not derivable from the
# code, so `test_the_direction_both_pages_claim_is_the_one_the_writer_enforces`
# holds the behaviour instead and this holds the two pages to one wording.
MCP_DIRECTION = "narrows its own authority and never widens it"

# Where a reader meets each statement. The heading, then the first thing under
# it: a paragraph in the README, a bullet in the safety model.
PAGES = {
    "README.md": ("README.md", "## Security by construction"),
    "docs/safety-model.md": ("docs/safety-model.md", "## Permissions and validation"),
}


def statement_under(document: Path, heading: str) -> str:
    """The one statement a reader meets under `heading`, as they meet it."""
    lines = document.read_text(encoding="utf-8").splitlines()
    headings = [index for index, line in enumerate(lines) if line.strip() == heading]
    assert len(headings) == 1, f"{document.name} has {len(headings)} {heading!r} headings; this test is not reading it any more"
    for line in lines[headings[0] + 1 :]:
        if line.strip():
            return line.strip()
    raise AssertionError(f"{document.name} says nothing under {heading!r}")


def statements() -> dict[str, str]:
    """Both security statements, by page.

    A source distribution's checkout has neither `docs/` nor a README beside
    these tests (MANIFEST.in ships only `docs/mcp-hosts.md`), so a missing
    document is skipped rather than failed. A repository checkout, which is what
    every contributor and this project's own CI run against, always has both.
    """
    found = {}
    for page, (relative, heading) in PAGES.items():
        document = REPO_ROOT / relative
        if document.is_file():
            found[page] = statement_under(document, heading)
    return found


def both_statements() -> dict[str, str]:
    """Both statements, or a skip: one page alone cannot be held against another."""
    found = statements()
    if len(found) != len(PAGES):
        pytest.skip("this is a checkout without both documents; the pages are compared where they both exist")
    return found


def generated_grants() -> dict[str, bool]:
    """Every permission a generation decides, by name, with the value it writes.

    Assembled from the three lists that own the answer rather than restated, so a
    permission joining any of them is one the documents may name from that
    moment, and one leaving is one they may not.
    """
    grants: dict[str, bool] = {}
    for section in GENERATED_WRITE_PERMISSIONS:
        grants.update(generated_permissions(section))
    grants.update(dict.fromkeys(GENERATED_PROJECT_PERMISSIONS, True))
    for flags in GENERATED_SECTION_PERMISSIONS.values():
        grants.update(dict.fromkeys(flags, True))
    return grants


def claimed_permissions(statement: str) -> list[str]:
    return sorted(set(PERMISSION_NAME.findall(statement)))


def cli_command(name: str) -> str:
    """`name` as a real subcommand of this CLI, spelled as the pages spell it.

    Parsed rather than looked up, because what a page tells an operator to type
    has to be what the parser accepts, key argument and all."""
    parsed = build_parser().parse_args([name, "debuggers.dut.allow_flash"])
    assert parsed.command == name
    assert parsed.keys == ["debuggers.dut.allow_flash"]
    return f"agentic-hil {name}"


def shared_claims() -> dict[str, str]:
    """What both pages must say, in the same words, from the same sources."""
    return {
        "the pair a generation holds false": f"{PAIR_CLAUSE} false",
        "the command that takes one grant back": f"{cli_command('revoke')} <key>",
        "the command that reopens it": f"{cli_command('grant')} <key>",
        "the direction an agent may move a permission": MCP_DIRECTION,
    }


@pytest.fixture
def generated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgenticHILConfig:
    """The file `agentic-hil init` writes on a bench with nothing attached, loaded.

    The documents describe a generated configuration, so the claims are checked
    against one, through the real writer and the real parser, rather than against
    a document a test built."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = init_config()
    assert result["ok"] is True, result
    return load_authoritative_config(workspace.resolve())


@pytest.mark.parametrize("page", sorted(PAGES))
def test_both_pages_carry_the_same_claims(page: str) -> None:
    """The drift itself: a fact one page states and the other does not."""
    statement = both_statements()[page]

    missing = {claim: expected for claim, expected in shared_claims().items() if expected not in statement}
    assert missing == {}, f"{page} does not carry {missing}, in: {statement}"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_page_names_a_permission_this_bench_does_not_have(page: str) -> None:
    """A name a reader could copy into `agentic-hil grant` and be refused for."""
    statement = both_statements()[page]

    grants = generated_grants()
    unknown = [name for name in claimed_permissions(statement) if name not in grants]
    assert unknown == [], f"{page} names {unknown}, which no generation decides"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_page_claims_a_grant_a_generation_withholds(page: str) -> None:
    """The overstatement, from the other side.

    Every permission either page names is one a generation grants, and the two
    it names as withheld are the interlocked pair, whole. A page that names one
    of the pair and not the other sends an operator to open the one flag that
    keeps flashing refused."""
    statement = both_statements()[page]

    grants = generated_grants()
    withheld = sorted(name for name in claimed_permissions(statement) if not grants[name])
    assert withheld == sorted(EXCLUSIVE_FLASH_PERMISSIONS), f"{page} presents {withheld} as the permissions a generation holds false"


def test_the_landing_page_names_no_permission_the_depth_page_leaves_out() -> None:
    """The README is the shorter half of one claim, never a second claim.

    It may name fewer permissions than the page it sends a reader to, and never
    one that page does not carry: that is what makes the two a split of the same
    statement rather than two statements that happen to agree today."""
    found = both_statements()

    landing = set(claimed_permissions(found["README.md"]))
    depth = set(claimed_permissions(found["docs/safety-model.md"]))
    assert landing <= depth, f"README.md names {sorted(landing - depth)}, which docs/safety-model.md does not"


def test_both_pages_name_the_refusal_an_undeclared_device_really_gets(generated_configuration: AgenticHILConfig) -> None:
    """The claim the correction rests on, read off the bench rather than off prose.

    Presence is the thing that is denied until it is declared, and this is the
    refusal a call gets for reaching past the declaration. Taken from a real
    resolution on a really generated file, so the pages name the error type an
    operator will actually see."""
    with pytest.raises(DeviceError) as refusal:
        resolve_device(generated_configuration, {"kind": "uart", "id": "a-port-this-configuration-does-not-declare"})

    error_type = refusal.value.result["error_type"]
    for page, statement in both_statements().items():
        assert error_type in statement, f"{page} does not name the refusal an undeclared device gets ({error_type}): {statement}"


def test_a_generated_configuration_carries_the_grants_the_pages_claim(generated_configuration: AgenticHILConfig) -> None:
    """The crossing: the list this file reads against the file a generation writes.

    `permission_summary` is what an operator is shown, so it is what the pages
    have to be true of. `allow_write` is the one name it cannot report here, and
    for the reason the pages give: a generation with nothing attached declares no
    port and no bus, so there is nothing for that grant to sit on. That absence
    is asserted rather than tolerated."""
    summary = permission_summary(generated_configuration)

    assert summary["com_ports"] == {}, "a generation with nothing attached declared a port"
    assert summary["can_buses"] == {}, "a generation with nothing attached declared a bus"

    written: dict[str, bool] = {}
    for key, value in summary.items():
        if isinstance(value, bool):
            written[key] = value
        elif key in ("debuggers", "com_ports", "can_buses"):
            for entry in value.values():
                written.update(entry)
        else:
            written.update(value)

    grants = generated_grants()
    assert set(written) == set(grants) - {"allow_write"}, "the generated file and the list this file reads name different permissions"
    assert written == {name: value for name, value in grants.items() if name in written}


def test_the_direction_both_pages_claim_is_the_one_the_writer_enforces() -> None:
    """`MCP_DIRECTION` is prose; this is the rule it describes.

    Turning a permission on is a widening whatever else the write carries, and
    turning one off is not. While that holds, both pages may keep the sentence."""
    key = "debuggers.dut.allow_flash"

    assert permission_widening({key: False}, {key: True}) == [key]
    assert permission_widening({key: True}, {key: False}) == []
