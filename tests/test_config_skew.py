"""What an installation does when the configuration in front of it is not its own.

From a bench where an older Agentic HIL was pointed at a file a newer one had
written.

The refusal named the field the unknown keys sat under and thirteen keys that
were allowed, and never the keys it had actually rejected, so an operator was
told their file was invalid when the truth was that a newer release wrote it. It
also carried no remediation, on a surface where every other refusal does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_hil import __version__
from agentic_hil.config import (
    FIELDS_INTRODUCED_IN,
    ConfigError,
    config_schema,
    release_order,
    validate_config_schema,
)
from agentic_hil.knowledge import ERROR_CATALOGUE, remediation_fields

# The three keys 0.21.3 added under a debuggers entry, which is what the bench
# that reported this was reading with 0.21.2.
NEWER_RELEASE_KEYS = ("discovered_by", "probe_inventory", "probe_inventory_note")


def schema_without(*fields: str) -> dict:
    """This configuration schema as a release that predates `fields` had it.

    How an older installation is reproduced without one: the bundled schema with
    the later release's additions taken back out of the debuggers entry. Every
    other rule the document is read under is this release's, so what the test
    exercises is the unknown-field path and nothing around it.
    """
    schema = config_schema()
    properties = schema["properties"]["debuggers"]["additionalProperties"]["properties"]
    for field in fields:
        assert field in properties, field
        del properties[field]
    return schema


def config_written_by_a_newer_release(workspace: Path) -> dict:
    return {
        "version": 3,
        "workspace_root": str(workspace),
        "state_root": str(workspace / "state"),
        "debuggers": {
            "dut": {
                "type": "openocd",
                "discovered_by": "usb_serial_inventory",
                "probe_inventory": "complete",
                "probe_inventory_note": "one probe attached",
            }
        },
    }


def refuse(raw: dict, monkeypatch: pytest.MonkeyPatch, *, schema: dict | None = None, version: str | None = None) -> dict:
    """The refusal `raw` produces, as the document every frontend serializes."""
    if schema is not None:
        monkeypatch.setattr("agentic_hil.config.config_schema", lambda: schema)
    if version is not None:
        monkeypatch.setattr("agentic_hil.config.__version__", version)
    with pytest.raises(ConfigError) as refusal:
        validate_config_schema(raw, "/projects/bench/config.yaml")
    return refusal.value.to_dict()


def test_an_unknown_field_refusal_names_the_keys_it_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#460: the rejected keys, not only the allowed ones.

    The bench refusal listed thirteen keys that were allowed and never the three
    it had thrown out, which is the one thing an operator needs in order to find
    them in their own file.
    """
    refusal = refuse(
        config_written_by_a_newer_release(tmp_path),
        monkeypatch,
        schema=schema_without(*NEWER_RELEASE_KEYS),
    )

    assert refusal["rejected_fields"] == sorted(NEWER_RELEASE_KEYS)
    for key in NEWER_RELEASE_KEYS:
        assert key in refusal["summary"], refusal["summary"]
    # The allowed list stays: TROUBLESHOOTING.md sends readers to it by name.
    assert "connect_mode" in refusal["allowed_fields"]


def test_keys_a_later_release_added_are_reported_as_a_newer_release_wrote_this(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#460: the case the bench actually hit, said in the words it deserves.

    Every rejected key is one this schema records as an addition of a release
    newer than the one running, so the file is not invalid: it was written by an
    Agentic HIL this installation has not caught up with. The next step is the
    upgrade, and the refusal has to name it, because an operator's own reading of
    "unknown field" is that they mistyped something.
    """
    refusal = refuse(
        config_written_by_a_newer_release(tmp_path),
        monkeypatch,
        schema=schema_without(*NEWER_RELEASE_KEYS),
        version="0.21.2",
    )

    assert "newer Agentic HIL" in refusal["summary"]
    assert refusal["written_by_release"] == "0.21.3"
    assert refusal["installed_version"] == "0.21.2"
    assert refusal["fields_introduced_in"] == dict.fromkeys(sorted(NEWER_RELEASE_KEYS), "0.21.3")
    assert "agentic-hil upgrade" in refusal["next_step"]


def test_the_file_saying_which_release_wrote_it_is_what_reaches_this_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#460's sentence could not fire on any build this project ships.

    A key reached it only if `FIELDS_INTRODUCED_IN` recorded it with a release
    above the running one, and a build knows only the fields it has: the keys a
    later release adds are exactly the keys the table has no entry for, and the
    two tests below pin every entry to at or under `__version__`. So the branch
    was unreachable, on a real installation, against a real file from a newer
    release, which is the one case it exists for.

    The file says which release wrote it, and that is read instead. No
    `__version__` is patched here and no schema is taken apart: this is the
    shipped build, refusing a key it does not have in a file that records a
    release above its own.
    """
    raw = config_written_by_a_newer_release(tmp_path)
    raw["debuggers"]["dut"] = {"type": "openocd", "a_field_from_the_future": True}
    raw["provenance"] = {"created_by": "agent", "agentic_hil_version": "99.0.0"}

    refusal = refuse(raw, monkeypatch)

    assert "newer Agentic HIL" in refusal["summary"]
    assert refusal["written_by_release"] == "99.0.0"
    assert refusal["installed_version"] == __version__
    assert refusal["rejected_fields"] == ["a_field_from_the_future"]
    assert "a_field_from_the_future" in refusal["summary"]
    assert "agentic-hil upgrade" in refusal["next_step"]
    # The table said nothing about this key, and did not have to.
    assert "fields_introduced_in" not in refusal


@pytest.mark.parametrize("recorded", ["0.0.1", __version__, "not a version", ""])
def test_a_file_from_this_release_or_an_older_one_keeps_the_refusal_about_the_typo(
    recorded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, which is the ordinary case: somebody mistyped a key.

    A file this installation or an older one wrote is not a file from the
    future, whatever unknown key it carries, and telling that operator to upgrade
    would send them away from the typo actually in their file. A provenance value
    that is not a release number answers nothing and falls to the same place.
    """
    raw = config_written_by_a_newer_release(tmp_path)
    raw["debuggers"]["dut"] = {"type": "openocd", "probe_idd": "PROBE-A"}
    raw["provenance"] = {"created_by": "human", "agentic_hil_version": recorded}

    refusal = refuse(raw, monkeypatch)

    assert "newer Agentic HIL" not in refusal["summary"]
    assert "written_by_release" not in refusal
    assert "next_step" not in refusal
    assert refusal["rejected_fields"] == ["probe_idd"]
    assert "probe_idd" in refusal["summary"]


def test_a_recorded_newer_release_outranks_the_table_that_cannot_know_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table is a refinement for a file that says nothing, not the judgement.

    Here the file records a release above this one and the rejected keys are ones
    the table happens to know about. The provenance decides, so the refusal is
    the upgrade one whatever the table would have made of the keys.
    """
    raw = config_written_by_a_newer_release(tmp_path)
    raw["debuggers"]["dut"]["probe_idd"] = "PROBE-A"
    raw["provenance"] = {"created_by": "agent", "agentic_hil_version": "99.0.0"}

    refusal = refuse(raw, monkeypatch, schema=schema_without(*NEWER_RELEASE_KEYS), version="0.21.2")

    assert "newer Agentic HIL" in refusal["summary"]
    assert refusal["written_by_release"] == "99.0.0"
    assert refusal["rejected_fields"] == sorted([*NEWER_RELEASE_KEYS, "probe_idd"])


def test_one_key_no_release_ever_added_keeps_the_refusal_about_the_typo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelling next to three real keys is not a newer release.

    The upgrade sentence is only true when every rejected key is one a later
    release added. One key that no release ever had makes it false, and a
    refusal that told this operator to upgrade would send them away from the typo
    that is actually in their file.
    """
    raw = config_written_by_a_newer_release(tmp_path)
    raw["debuggers"]["dut"]["probe_idd"] = "PROBE-A"

    refusal = refuse(raw, monkeypatch, schema=schema_without(*NEWER_RELEASE_KEYS), version="0.21.2")

    assert "newer Agentic HIL" not in refusal["summary"]
    assert "written_by_release" not in refusal
    assert "next_step" not in refusal
    assert refusal["rejected_fields"] == sorted([*NEWER_RELEASE_KEYS, "probe_idd"])
    assert "probe_idd" in refusal["summary"]


def test_an_installation_new_enough_for_the_keys_never_calls_them_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direction the table is read in.

    These same three keys under a section that never had them are not a lagging
    installation, so the release comparison has to be a comparison and not
    membership in the table.
    """
    refusal = refuse(
        config_written_by_a_newer_release(tmp_path),
        monkeypatch,
        schema=schema_without(*NEWER_RELEASE_KEYS),
        version="0.22.0",
    )

    assert "newer Agentic HIL" not in refusal["summary"]
    assert "written_by_release" not in refusal
    assert refusal["rejected_fields"] == sorted(NEWER_RELEASE_KEYS)


def test_the_release_table_names_only_fields_this_schema_has() -> None:
    """A table entry for a key the schema does not define would be advice about nothing.

    The table is the schema's own record of when each field arrived, so an entry
    that no longer matches a property has outlived what it described, and this is
    what says so rather than a refusal quoting a release for a key nobody can
    write.
    """
    schema = config_schema()
    containers = {"debuggers.*": schema["properties"]["debuggers"]["additionalProperties"]["properties"]}

    assert set(FIELDS_INTRODUCED_IN) <= set(containers), FIELDS_INTRODUCED_IN
    for container, fields in FIELDS_INTRODUCED_IN.items():
        for field, release in fields.items():
            assert field in containers[container], f"{container}.{field}"
            # An entry claiming a release this installation predates would make
            # every refusal it touches say the file came from the future.
            assert release_order(release) <= release_order(__version__), f"{container}.{field}"


def test_every_config_invalid_refusal_carries_the_catalogues_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#460: the remediation block every other refusal on this surface carries.

    `config_invalid` is the refusal an operator meets before any tool exists to
    call, and it was the one that went out with no way forward attached. The
    entry has to name both causes, because the two have opposite fixes and the
    refusal cannot always tell which one it is looking at.
    """
    refusal = refuse(
        config_written_by_a_newer_release(tmp_path),
        monkeypatch,
        schema=schema_without(*NEWER_RELEASE_KEYS),
    )

    assert refusal["remediation"] == remediation_fields("config_invalid")["remediation"]
    assert refusal["do_not"] == remediation_fields("config_invalid")["do_not"]
    said = json.dumps(ERROR_CATALOGUE["config_invalid"].as_json())
    assert "agentic-hil upgrade" in said
    assert "rejected_fields" in said and "allowed_fields" in said


def test_a_refusal_that_is_not_about_fields_at_all_still_carries_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `config_invalid`, not only the unknown-field one.

    A wrong type and an unsupported value land on the same error type and the
    same reader, so an entry that only reached one branch of the refusal would be
    a fix that appears and disappears for no reason a caller can see.
    """
    raw = config_written_by_a_newer_release(tmp_path)
    raw["debuggers"]["dut"]["timeout_s"] = "soon"

    refusal = refuse(raw, monkeypatch)

    assert refusal["error_type"] == "config_invalid"
    assert refusal["summary"] == "debuggers.dut.timeout_s has the wrong type."
    assert refusal["remediation"] == remediation_fields("config_invalid")["remediation"]


def test_unknown_keys_are_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strictness #460 does not touch.

    Only the wording and the remediation change. A key this schema does not
    define still stops the load, on the newer-release branch as much as on the
    typo one, because a configuration read past in silence is a policy nobody
    wrote being enforced.
    """
    monkeypatch.setattr("agentic_hil.config.config_schema", lambda: schema_without(*NEWER_RELEASE_KEYS))
    for version in ("0.21.2", "0.22.0"):
        monkeypatch.setattr("agentic_hil.config.__version__", version)
        with pytest.raises(ConfigError) as refusal:
            validate_config_schema(config_written_by_a_newer_release(tmp_path), "/projects/bench/config.yaml")
        assert refusal.value.error_type == "config_invalid"
