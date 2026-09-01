"""A per-user tree that opens as one place and resolves to another.

Found on a Windows 11 bench whose agent host runs inside an MSIX AppContainer:
the container virtualizes ``%APPDATA%`` and ``%LOCALAPPDATA%``, so a path under
either creates, opens and writes exactly as it reads while ``resolve`` maps it
onto the package's private ``LocalCache`` tree. Nothing on the chain looks
unusual to any of the checks that walk it: no reparse point, no symlink, link
count one, and ``samestat`` holds, because the indirection lives in name
resolution alone.

The whole defect follows from one disagreement. ``safe_writable_directory``
accepts such a directory, because it really is creatable and really is writable,
and ``safe_file_path`` refuses every file under it, because the parent does not
resolve to itself. Generation selected on the first and every later write met the
second, so ``agentic-hil init`` wrote a ``state_root`` the bench then refused,
and refused hard enough that the call which would replace it was refused too.

No AppContainer is needed to hold the code to this. ``resolve`` is the seam the
container acts on, so a stand-in that remaps one chosen tree reproduces the
profile exactly, on either platform, with nothing timing-dependent in it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agentic_hil.bench import BenchMutex
from agentic_hil.cli import (
    _claude_code_deny_patterns,
    _configuration_the_projects_walk_finds,
    _external_project_record_path,
    _recorded_external_configurations,
    _visible_project_configurations,
    init_project,
    restrict_agent_write_access,
    uninstall_agent_integration,
)
from agentic_hil.config import (
    CONFIG_ENV,
    ConfigError,
    absolute_without_symlinks,
    atomic_write_bytes,
    atomic_write_text,
    authoritative_config_target,
    load_authoritative_config,
    project_config_directories,
    project_config_directory,
    project_config_leaf,
    project_config_path,
    provisionable_state_root,
    resolve_stable_directory,
    safe_append_text,
    safe_file_lock,
    safe_file_path,
    safe_read_bytes,
    safe_read_text,
    safe_writable_directory,
    secure_optional_read_bytes,
    secure_optional_read_text,
    user_state_root,
)
from agentic_hil.configwrite import config_document_snapshot, load_config_document
from agentic_hil.report import report_state_path
from agentic_hil.tools import (
    PROJECT_CONFIG_CREATE,
    AgenticHILToolService,
    UnprovisionedToolService,
    audit_gated_tools,
    audited_hardware_tools,
)
from agentic_hil.types import fold_hardware_id
from tests.test_agent_provisioning import attached_hardware, bench, written_document


def virtualize(monkeypatch: pytest.MonkeyPatch, source: Path, destination: Path) -> None:
    """Make ``source`` resolve onto ``destination``, and nothing else move.

    The stand-in for AppContainer path virtualization. Only ``resolve`` is
    touched, which is exactly the reach the container has: ``lstat``, the reparse
    attributes, the link count and ``samestat`` all keep answering about the
    spelling that was opened, and every other tree on the machine resolves as it
    did. ``destination`` need not exist; the container's backing tree is created
    on first write and the checks under test never look inside it.

    Only a path that *exists* moves, which is the detail the whole profile turns
    on. The redirect is what the package's writes land in, so a directory that
    has not been created yet still resolves to the spelling it was named by, and
    the same directory one ``mkdir`` later resolves into the private tree. That
    is why a check computing a path before creating it agrees with itself and the
    write that follows does not, and modelling it any other way makes the two
    agree and reproduces nothing.
    """
    real = Path.resolve
    prefix = os.path.normcase(str(source))

    def resolve(self: Path, strict: bool = False) -> Path:
        resolved = real(self, strict=strict)
        text = os.path.normcase(str(resolved))
        if not os.path.lexists(resolved):
            return resolved
        if text == prefix or text.startswith(prefix + os.sep):
            return Path(str(destination) + str(resolved)[len(str(source)) :])
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve)


def virtualized_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point this profile's state root at a tree the container redirects."""
    virtual = tmp_path / "virtual-localappdata"
    virtual.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(virtual))
    monkeypatch.setenv("XDG_STATE_HOME", str(virtual))
    return virtual


def fallback_state_root() -> Path:
    return Path.home() / ".agentic-hil" / "state"


# ---------------------------------------------------------------------------
# The disagreement itself, stated as the two checks answering about one path.


def test_a_writable_directory_can_still_be_one_every_write_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why selecting on writability alone was never enough.

    Both answers here are correct about the thing each asks. The directory is
    creatable and writable, and it is also not the spelling it resolves to. A
    generation that asked only the first question wrote down a path the second
    question refuses.
    """
    virtual = virtualized_user_state(tmp_path, monkeypatch)
    candidate = virtual / "agentic-hil"
    virtualize(monkeypatch, virtual, tmp_path / "package-local-cache")

    assert safe_writable_directory(candidate, field="state_root") == candidate

    with pytest.raises(ConfigError) as refusal:
        resolve_stable_directory(candidate, field="state_root")

    assert refusal.value.error_type == "unsafe_configured_path"
    # Both spellings, because either one alone is what cost the bench an hour:
    # the configured path looks clean when it is walked, and the resolved one is
    # the answer, being the spelling that works.
    assert refusal.value.details["path"] == str(candidate)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-local-cache" / "agentic-hil")
    assert refusal.value.details["field"] == "state_root"


def test_a_real_directory_passes_the_resolve_identity_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check is about redirection, not about being under AppData."""
    plain = tmp_path / "ordinary" / "agentic-hil"

    assert resolve_stable_directory(safe_writable_directory(plain, field="state_root"), field="state_root") == plain


# ---------------------------------------------------------------------------
# #353: generation never writes a state_root the enforcer will refuse.


def test_generation_falls_through_a_virtualized_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented default is writable here and still unusable.

    So the candidate walk has to reject it on the test a later write applies, and
    take the fallback it already has, rather than write the spelling down and
    have the bench discover the disagreement at its first report.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_state(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-local-cache")
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert created["ok"] is True, created
    assert provisionable_state_root(workspace) == fallback_state_root()
    assert Path(created["state_root"]) == fallback_state_root()
    assert written_document(created)["state_root"] == str(fallback_state_root())
    assert user_state_root() == virtual / "agentic-hil", "the default was reachable and was passed over on merit"


def test_a_generated_state_root_survives_the_enforcer_that_refused_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the fall-through, stated as the bench being usable.

    The generated file loads, and the audit trail the first hardware action needs
    is writable under the root it names, which is the whole property the earlier
    selection lost.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_state(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-local-cache")
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        assert service.call(PROJECT_CONFIG_CREATE)["ok"] is True
        probed = service.call("probe_target")
    finally:
        service.close()

    assert probed.get("error_type") != "audit_unavailable", probed
    assert Path(load_authoritative_config(workspace).state_root) == fallback_state_root()


def test_no_usable_state_root_still_refuses_with_the_way_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fall-through is a fallback, not a promise that one always exists.

    Where both candidates are virtualized there is nothing left to pick, and what
    the caller gets is the refusal carrying the remediation, not a silent write.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_state(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-local-cache")
    virtualize(monkeypatch, Path.home(), tmp_path / "package-home-cache")

    with pytest.raises(ConfigError) as refusal:
        provisionable_state_root(workspace)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"].startswith(str(tmp_path / "package-home-cache"))


# ---------------------------------------------------------------------------
# #353: the regeneration path out is not gated by the thing it repairs.


def test_project_config_create_is_not_gated_on_the_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken state_root must not guard the call that replaces it.

    Every hardware action is refused here, and correctly: nothing may touch the
    board while what happened cannot be recorded. Generation is the exception,
    because `ensure_audit_ready` writes under exactly the root that is broken, so
    gating it made the bench unable to regenerate its way out and left
    hand-editing the authoritative file as the only route, which is the one thing
    the doctrine forbids.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created

    # The bench is generated and healthy, and then its state root becomes the
    # virtualized spelling underneath it, which is the order the real profile
    # produced: the file was written by a host outside the container, or by one
    # whose redirection began afterwards.
    state_root = Path(created["state_root"])
    virtualize(monkeypatch, state_root, tmp_path / "package-local-cache")

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call("probe_target")
        regenerated = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert refused["error_type"] == "audit_unavailable", refused
    assert refused["audit_error"]["error_type"] == "unsafe_configured_path"
    assert regenerated.get("error_type") != "audit_unavailable", regenerated
    assert regenerated["ok"] is True, regenerated
    # The one read of this bench with no record of it says so in its own result.
    # A repair that went quiet about that would be the same silence the defect
    # was made of.
    assert regenerated["hardware_read_audited"] is False
    assert regenerated["hardware_read_unaudited_reason"] == "unsafe_configured_path"
    # And it did not regenerate back onto the broken root: the same candidate
    # walk that refuses it at selection refuses it here too.
    assert Path(regenerated["state_root"]) != state_root
    assert yaml.safe_load(Path(regenerated["path"]).read_text(encoding="utf-8"))["state_root"] == regenerated["state_root"]


def test_every_other_audited_tool_still_proves_its_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exemption is one call wide, and it is named rather than inferred."""
    assert audited_hardware_tools() - audit_gated_tools() == {PROJECT_CONFIG_CREATE}
    assert PROJECT_CONFIG_CREATE in audited_hardware_tools(), "still blocked while the bench is quarantined"


# ---------------------------------------------------------------------------
# Review round 0: the exemption is for a broken `state_root`, and only that. It
# does not become a way around the audit gate for a failure a regeneration will
# not repair, and it does not drop the configured device locks the leased path
# holds.


def test_a_corrupt_report_state_is_not_read_around_by_regeneration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit gate holds for an integrity failure a regeneration cannot repair.

    `ensure_audit_ready` refuses two ways and only one, the `state_root` spelling
    the enforcer will not accept, is the one a regeneration replaces. A corrupt
    `report-state.json` under a `state_root` that resolves cleanly is the other:
    `config_invalid`, which the same regeneration leaves exactly in place, because
    it selects the same healthy root and rewrites nothing under it. Reading the
    board around it would bypass the gate for an integrity failure and report a
    repair that never lands, the next `probe_target` would meet the same wall.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created

    # A healthy state_root with corrupt content under it: the file is there and it
    # is not JSON, so `ensure_audit_ready` refuses with `config_invalid`, not with
    # the `unsafe_configured_path` the repair path is for.
    config = load_authoritative_config(workspace)
    Path(report_state_path(config)).write_text("{ this is not report state", encoding="utf-8")

    service = AgenticHILToolService(config, frontend="mcp")
    try:
        refused = service.call("probe_target")
        regenerated = service.call(PROJECT_CONFIG_CREATE)
        still_refused = service.call("probe_target")
    finally:
        service.close()

    # The ordinary hardware call is refused, correctly, and names the integrity
    # failure underneath the audit gate.
    assert refused["error_type"] == "audit_unavailable", refused
    assert refused["audit_error"]["error_type"] == "config_invalid"
    # And so is the regeneration: it is not read around, and it does not claim to
    # have repaired anything. No unaudited-read note is attached, because no
    # unaudited read happened.
    assert regenerated["ok"] is False, regenerated
    assert regenerated["error_type"] == "audit_unavailable", regenerated
    assert regenerated["audit_error"]["error_type"] == "config_invalid"
    assert "hardware_read_unaudited_reason" not in regenerated
    assert "hardware_read_audited" not in regenerated
    # The gate is still shut afterwards, which is the whole proof: a regeneration
    # that had read the board and reported success would have left it open.
    assert still_refused["error_type"] == "audit_unavailable", still_refused


def test_the_audit_repair_read_still_holds_the_configured_resource_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken `state_root` does not license reading past a board's alias lock.

    `resource_id` is the canonical alias a run holds a board through, and a
    debugger can carry it with `probe_id` still null, so the enumerated
    `probe:<serial>` lock the read takes never covers `physical:<resource_id>`.
    The leased path acquires that alias in its own right; the audit-repair read
    has no `state_root` to lease under but must acquire it just the same, or it
    reads a board another owner is holding and rewrites the configuration under
    the live owner (review round 0, finding 2).
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created

    # The alias set the reviewer's reproduction used: `resource_id` present,
    # `probe_id` cleared, so the only lock that protects this board is the one the
    # enumeration cannot derive.
    path = Path(created["path"])
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["resource_id"] = "bench-a"
    document["debuggers"]["dut"].pop("probe_id", None)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    # Break the state_root so the read takes the audit-repair branch, and hold the
    # board's alias from another session before it does.
    state_root = Path(created["state_root"])
    virtualize(monkeypatch, state_root, tmp_path / "package-local-cache")
    before = path.read_bytes()

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"physical:{fold_hardware_id('bench-a')}"])
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
        stranger.release_all()

    # The repair read is refused exactly as the leased path refuses it, names the
    # holder, and writes nothing over the board another owner is on.
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["holder"]["label"] == "other-bench-session"
    assert refused["side_effect_committed"] is False
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# #354: the configuration's own location has the candidate walk too.


def virtualized_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point this profile's config root at a tree the container redirects."""
    virtual = tmp_path / "virtual-appdata"
    virtual.mkdir()
    monkeypatch.setenv("APPDATA", str(virtual))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(virtual))
    return virtual


def fallback_config_root() -> Path:
    return Path.home() / ".agentic-hil" / "projects"


def test_generation_falls_through_a_virtualized_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`state_root` learned this and the configuration's own path had not.

    So a stock installation inside any MSIX-packaged host dead-ended: nothing
    could be generated at all, and `AGENTIC_HIL_CONFIG` was the only lever, which
    is a lever a stranger does not find.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert created["ok"] is True, created
    assert Path(created["path"]).parent.parent == fallback_config_root()
    # The default was tried first and on merit: the walk got as far as creating
    # the per-workspace directory under the virtualized root, then found it
    # resolving into the package tree and moved on without writing a file into
    # it. That directory being there and empty is the evidence.
    attempted = virtual / "agentic-hil" / "projects" / project_config_leaf(workspace)
    assert attempted.is_dir()
    assert not (attempted / "config.yaml").exists()


def test_a_configuration_under_the_fallback_root_is_found_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file nothing can load again would be worse than the dead end it fixes.

    So discovery has the same candidate list the creation does, and a bench
    generated under the fallback root loads, binds and reaches hardware from a
    fresh server exactly as one under the platform default does.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert project_config_path(workspace) == Path(created["path"])
    loaded = load_authoritative_config(workspace)
    assert loaded.config_path == created["path"]
    assert Path(loaded.config_path).parent.parent == fallback_config_root()


def test_the_default_config_root_is_still_preferred_when_it_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is a fallback, not a relocation of every profile's config."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert Path(created["path"]).parent.parent == project_config_directory()
    assert project_config_directories()[0] == project_config_directory()
    assert not fallback_config_root().exists()


def test_the_projects_walk_finds_what_the_fallback_root_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configuration nothing on disk can find is what #246 was about.

    A second root would have reintroduced exactly that, so the walk every
    listing, agent-rule refresh and uninstall reads covers both.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    found = [path for path, _state_root in _visible_project_configurations()]

    assert Path(created["path"]) in found
    assert _configuration_the_projects_walk_finds(Path(created["path"])) is True


# ---------------------------------------------------------------------------
# #354: the refusal names both spellings and stops sending people after symlinks.


def test_the_refusal_carries_the_resolved_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One line instead of an hour of archaeology.

    The chain shows no symlink, no reparse point, nlink 1 everywhere, and
    samestat holds. Whoever debugs that walks it, finds it clean and is stuck, so
    the refusal has to hand over the spelling it compared against.
    """
    virtual = tmp_path / "virtual-appdata"
    virtual.mkdir()
    backing = tmp_path / "package-roaming-cache"
    target = virtual / "agentic-hil" / "projects" / "config.yaml"
    target.parent.mkdir(parents=True)
    virtualize(monkeypatch, virtual, backing)

    with pytest.raises(ConfigError) as refusal:
        safe_file_path(target)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(target)
    assert refusal.value.details["resolved_parent"] == str(backing / "agentic-hil" / "projects")
    # And it no longer asserts a symlink at a chain that has none.
    assert "symlink" not in refusal.value.summary
    remediation = " ".join(refusal.value.to_dict()["remediation"])
    assert "resolved_parent" in remediation
    assert str(Path.home() / ".agentic-hil") in remediation


def test_a_genuine_symlinked_parent_is_still_covered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The symlink framing stays where a symlink is what is actually there.

    Same error type, same refusal, and `resolved_parent` now names where the link
    goes, which was the one thing the old message asked the reader to work out.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let this process create a symlink")

    with pytest.raises(ConfigError) as refusal:
        safe_file_path(link / "config.yaml")

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(real)


def test_the_single_link_regular_file_refusal_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Splitting the two conditions must not drop either of them."""
    directory = tmp_path / "holder"
    directory.mkdir()
    not_a_file = directory / "config.yaml"
    not_a_file.mkdir()

    with pytest.raises(ConfigError) as refusal:
        safe_file_path(not_a_file)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert "single-link regular file" in refusal.value.summary
    assert "resolved_parent" not in refusal.value.details


def test_a_configuration_in_the_fallback_root_stays_the_authoritative_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing file outranks a candidate root that could be created now.

    The virtualization is what put this bench's configuration under the fallback
    root, and a host outside the container asks the same question with the
    platform default perfectly usable. Answering with the default there would
    have `project_config_set` refuse every write as not this workspace's
    authoritative file, and `init --force` generate a second configuration beside
    the one in force.
    """
    workspace = bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    # Only the redirection is scoped, so leaving the block is the host reading
    # the same profile from outside the container. Everything else about the
    # environment, the config root included, stays exactly as it was.
    with pytest.MonkeyPatch.context() as container:
        virtualize(container, virtual, tmp_path / "package-roaming-cache")
        service = UnprovisionedToolService(workspace)
        try:
            created = service.call(PROJECT_CONFIG_CREATE)
        finally:
            service.close()
    assert Path(created["path"]).parent.parent == fallback_config_root()

    # The default root is reachable and resolve-stable again, and the file that
    # is in force has not moved.
    assert project_config_directory().is_dir(), "the default root is usable here"
    assert authoritative_config_target(workspace) == Path(created["path"])
    assert project_config_path(workspace) == Path(created["path"])


# ---------------------------------------------------------------------------
# #358: the record of the projects the projects directory does not hold has the
# same walk, because it is placed against the same roots.


def external_project_records() -> tuple[Path, Path]:
    """Both spellings of this user's record, best first.

    Written out rather than derived from the code under test, for the reason
    `tests/test_agentic_hil.py` spells the platform default one out: this is a
    file on an operator's disk, and a release that moved it quietly would leave
    every project an earlier release recorded unfindable, which is the whole of
    #246.
    """
    return (
        project_config_directory().parent / "external-projects.json",
        fallback_config_root().parent / "external-projects.json",
    )


def claude_settings(deny: list[str]) -> Path:
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"deny": deny}}) + "\n", encoding="utf-8")
    return settings


def deny_rules(settings: Path) -> list[str]:
    return json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]


def bound_project(monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration `AGENTIC_HIL_CONFIG` binds outside the projects directory."""
    bound = Path.home() / "operator-policy" / "config.yaml"
    monkeypatch.setenv(CONFIG_ENV, str(bound))
    return bound


def test_the_project_record_falls_through_a_virtualized_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`init --agent` on the profile this walk exists for, end to end.

    A project `AGENTIC_HIL_CONFIG` binds is exactly the one that needs the
    record: nothing else on disk says it is there. With the platform default
    virtualized, the write beside it is refused, and refusing to record is
    refusing to write the rule by design, so the command that sets a bench up
    could not finish at all on a host whose profile directories are redirected.
    """
    bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["agent_write_restriction"]["ok"] is True, result["steps"]
    default_record, fallback_record = external_project_records()
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": [str(bound)]}
    # The default root was tried first and on merit, exactly as the
    # configuration's own target tries it: the directory is there and holds no
    # record, because the walk found it resolving into the package tree and
    # moved on without writing a file into it.
    assert (virtual / "agentic-hil").is_dir()
    assert not default_record.exists()
    # And what was written is what every later reader answers with.
    assert _external_project_record_path() == fallback_record
    assert _recorded_external_configurations() == [bound]


def test_init_migrates_a_record_off_a_virtualized_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing default record does not dead-end the fallback it needs (#358).

    The profile the fallback exists for is precisely the one an earlier,
    unpackaged host already left a record on: `%APPDATA%\\agentic-hil\\external-projects.json`
    is on disk, and inside the container it resolves into the package's private
    tree. Returning it as the write target, because it exists, locked a file
    whose parent `safe_file_path` refuses, so `init --agent claude-code` failed
    before it wrote a byte, in the very profile the walk was added to support.

    The existing but unwritable record is left exactly as it is, its one entry
    carried across into the record beside the fallback root alongside the new one,
    and that safe record is the one every later reader answers with.
    """
    bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    prior = Path.home() / "operator-policy" / "legacy" / "config.yaml"
    default_record.parent.mkdir(parents=True, exist_ok=True)
    default_record.write_text(json.dumps({"configurations": [str(prior)]}) + "\n", encoding="utf-8")
    # Virtualize only after the record is on disk: the container redirects a path
    # that exists, so this reproduces the unpackaged host's file seen from inside
    # the package rather than one written there now.
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["agent_write_restriction"]["ok"] is True, result["steps"]
    # The unsafe default is not the write target and not touched: a write to it is
    # refused, and its bytes are exactly what they were.
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": [str(prior)]}
    # Its entry migrated across, beside the new one, into the record a write lands
    # in, read through the default before the write and combined with it.
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(prior), str(bound)])}
    # And that safe record is what every later reader answers with, so neither the
    # migrated project nor the new one goes missing.
    assert _external_project_record_path() == fallback_record
    assert sorted(_recorded_external_configurations()) == sorted([bound, prior])


def test_the_default_record_location_is_unchanged_where_it_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is a fallback, not a relocation of every profile's record.

    Holds both ways, and is pinned for that: this is where every earlier release
    wrote, where an operator goes looking, and where a record an earlier release
    left is still read from.
    """
    bench(tmp_path, monkeypatch)
    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    default_record, fallback_record = external_project_records()
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": [str(bound)]}
    assert not fallback_record.exists()


def test_a_record_beside_the_fallback_root_is_the_one_a_reader_answers_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record nothing reads again is the dead end it was written to prevent.

    Every reader of this file walks both roots, the way the projects listing
    does, or a run that had to write beside the fallback root would be read by
    the next one as a user with no externally bound projects at all: the rule
    for the bench it did record would be taken back as a leftover, and the
    uninstall accounting would stop naming its trees.
    """
    bench(tmp_path, monkeypatch)
    bound = Path.home() / "operator-policy" / "config.yaml"
    default_record, fallback_record = external_project_records()
    fallback_record.parent.mkdir(parents=True, exist_ok=True)
    fallback_record.write_text(json.dumps({"configurations": [str(bound)]}) + "\n", encoding="utf-8")

    assert not default_record.exists()
    assert _external_project_record_path() == fallback_record
    assert _recorded_external_configurations() == [bound]


def test_a_record_beside_the_fallback_root_stays_the_one_that_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing record outranks a root that could be created now.

    The virtualization is what put a record beside the fallback root, and the
    host outside the container asks the same question with the platform default
    perfectly usable. A second record there would hold half of this user's
    externally bound projects while every reader answered out of the other half.
    """
    bench(tmp_path, monkeypatch)
    first = Path.home() / "benches" / "alpha" / "config.yaml"
    second = Path.home() / "benches" / "beta" / "config.yaml"
    default_record, fallback_record = external_project_records()
    fallback_record.parent.mkdir(parents=True, exist_ok=True)
    fallback_record.write_text(json.dumps({"configurations": [str(first)]}) + "\n", encoding="utf-8")
    settings = claude_settings(["Bash(curl *)"])

    restriction = restrict_agent_write_access("claude-code", second, Path.home() / ".agentic-hil" / "state")

    assert restriction["ok"] is True, restriction
    # Nothing is redirected here, so the platform default passes the very test
    # the walk selects on and could hold the record right now. The entry goes to
    # the file that is in force anyway, and no second record appears beside it.
    assert resolve_stable_directory(safe_writable_directory(default_record.parent, field="config_path"), field="config_path") == default_record.parent
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(first), str(second)])}
    assert not default_record.exists()
    assert deny_rules(settings)[0] == "Bash(curl *)"


def test_a_record_under_both_roots_answers_with_the_platform_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One file is read from and written to, but every project either names counts.

    The reader still answers with the first candidate that exists, the platform
    default, the way `project_config_path` picks one configuration, and a run's
    new entry still lands in that one file, never a merge of two writable ones.
    What the wanted set is derived from is a different question, and it is the
    union of both: a project named only beside the fallback root is one this tool
    recorded just the same, and dropping it from what a refresh accounts for is
    how the reverse migration lost projects (round 3, finding 1). So the write
    also carries the union onto the winner, converging the two records rather than
    leaving one holding half.
    """
    bench(tmp_path, monkeypatch)
    canonical = Path.home() / "benches" / "canonical" / "config.yaml"
    beside = Path.home() / "benches" / "beside" / "config.yaml"
    added = Path.home() / "benches" / "added" / "config.yaml"
    default_record, fallback_record = external_project_records()
    for record, entry in ((default_record, canonical), (fallback_record, beside)):
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"configurations": [str(entry)]}) + "\n", encoding="utf-8")
    settings = claude_settings(["Bash(curl *)"])

    # The reader answers with one file, but the recorded set unions both, so the
    # project named only beside the fallback root is not dropped.
    assert _external_project_record_path() == default_record
    assert sorted(_recorded_external_configurations()) == sorted([canonical, beside])

    restriction = restrict_agent_write_access("claude-code", added, Path.home() / ".agentic-hil" / "state")

    assert restriction["ok"] is True, restriction
    # The winner converges to the full union plus the new entry: its own, the
    # fallback's, and the one just added.
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(canonical), str(beside), str(added)])}
    # The one that did not win is not written through or removed; the writer only
    # ever appends to the winner, so it keeps its own copy.
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": [str(beside)]}
    assert deny_rules(settings)[0] == "Bash(curl *)"


def test_a_failed_project_half_takes_back_the_record_it_wrote_beside_the_fallback_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollback set follows the record to whichever root it lands in.

    `_project_mutation_paths` snapshots this file so a failed project half never
    leaves an entry standing for a project whose setup was reversed. Snapshotting
    the reader's answer would have covered the platform default while the write
    went beside the fallback root, so the run would report its changes taken back
    with the new entry still there, which a later refresh reads as a
    configuration that has gone missing.
    """
    from agentic_hil import cli as cli_module

    bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])
    fallback_record = external_project_records()[1]
    real_restrict = cli_module.restrict_agent_write_access

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        written = real_restrict(*args, **kwargs)
        assert written["ok"] is True, written
        # The record the real step just wrote is what the rollback must not leave.
        assert fallback_record.is_file()
        return {"ok": False, "error_type": "injected_failure", "summary": "late restriction failure"}

    monkeypatch.setattr("agentic_hil.cli.restrict_agent_write_access", write_then_fail)

    result = init_project(agent="claude-code")

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True, result["rollback"]
    assert not fallback_record.exists()
    assert not bound.exists()


def test_uninstall_takes_back_the_record_under_either_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file this installation wrote is what the command exists to take back.

    Which root holds it depends on what the profile could write when the project
    was set up, and a release before this walk existed could only ever have
    written beside the platform default. Both spellings go, or an uninstall on
    the profile the walk exists for leaves the record standing in a directory of
    its own.
    """
    bench(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    bound = Path.home() / "benches" / "alpha" / "config.yaml"
    bound.parent.mkdir(parents=True)
    bound.write_text(f"state_root: {(Path.home() / '.agentic-hil' / 'state').as_posix()!r}\n", encoding="utf-8")
    for record in (default_record, fallback_record):
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"configurations": [str(bound)]}) + "\n", encoding="utf-8")
    settings = claude_settings(["Bash(curl *)"])
    assert restrict_agent_write_access("claude-code", bound, Path.home() / ".agentic-hil" / "state")["ok"] is True

    result = uninstall_agent_integration(["claude-code"])

    assert result["ok"] is True, result
    assert deny_rules(settings) == ["Bash(curl *)"]
    assert [item["what"] for item in result["removed"]].count("project record") == 2
    assert not default_record.exists()
    assert not fallback_record.exists()
    # What the record named is still standing, which is what `kept` promises.
    assert bound.is_file()


# ---------------------------------------------------------------------------
# Review round 2: the write target has to prove more than resolve-identity, the
# migration has to keep every earlier record's entries, and uninstall must not
# crash on the record the migration deliberately leaves behind.


def reject_write_probe(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Make one directory refuse the create-and-delete probe, nothing else.

    The stand-in for a read-only mount, a deny ACE or a filter driver: the
    directory resolves to itself and reads fine, and only `safe_writable_directory`
    turns it down, which is the half `resolve_stable_directory` never asks. Scoped
    to the one directory so every other write in `init` behaves normally.
    """
    from agentic_hil import cli as cli_module

    real = cli_module.safe_writable_directory
    refused = os.path.normcase(str(absolute_without_symlinks(directory)))

    def probe(target: str | Path, *, field: str, config_path: str | None = None) -> Path:
        if os.path.normcase(str(absolute_without_symlinks(Path(target)))) == refused:
            raise ConfigError("unsafe_configured_path", "Configured directory exists and cannot be written by this process.", {"field": field, "path": str(target)})
        return real(target, field=field, config_path=config_path)

    monkeypatch.setattr("agentic_hil.cli.safe_writable_directory", probe)


def test_init_migrates_a_record_off_a_write_refused_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve-identity was never the whole of it (round 2, finding 1).

    An existing default record whose parent resolves to itself and still refuses
    every write, a read-only mount, a deny ACE, a filter driver, passed the
    resolve-identity check and was taken as the write target, so `init` locked it
    and dead-ended on the sidecar lock beside it before the writable fallback was
    ever reached. The full `safe_writable_directory` check turns it down here, and
    the record migrates to the fallback exactly as the virtualized one does.
    """
    bench(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    prior = Path.home() / "operator-policy" / "legacy" / "config.yaml"
    default_record.parent.mkdir(parents=True, exist_ok=True)
    default_record.write_text(json.dumps({"configurations": [str(prior)]}) + "\n", encoding="utf-8")
    # The default record's own parent is the only directory that refuses a write;
    # the fallback root, and everything else `init` touches, stay writable.
    reject_write_probe(monkeypatch, default_record.parent)
    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["agent_write_restriction"]["ok"] is True, result["steps"]
    # The write-refused default is not the target and not touched.
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": [str(prior)]}
    # Its entry carried across, beside the new one, into the record a write lands in.
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(prior), str(bound)])}
    assert _external_project_record_path() == fallback_record
    assert sorted(_recorded_external_configurations()) == sorted([bound, prior])


def test_init_unions_a_pre_existing_fallback_with_the_virtualized_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fallback that already exists must not drop the default's entries (finding 2).

    The profile the fallback exists for can already hold a record beside it, from
    an earlier `init` on this same host. When it does, the reader answers with the
    fallback and the virtualized default is passed over, and reading only the
    fallback lost every project the default alone still named, which a later
    refresh then took the rules back for. The union across both records is what
    keeps them.
    """
    bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    project_a = Path.home() / "operator-policy" / "alpha" / "config.yaml"
    project_b = Path.home() / "operator-policy" / "beta" / "config.yaml"
    default_record.parent.mkdir(parents=True, exist_ok=True)
    default_record.write_text(json.dumps({"configurations": [str(project_a)]}) + "\n", encoding="utf-8")
    fallback_record.parent.mkdir(parents=True, exist_ok=True)
    fallback_record.write_text(json.dumps({"configurations": [str(project_b)]}) + "\n", encoding="utf-8")
    # Virtualize only after both records are on disk, so the default is the file
    # an unpackaged host left, seen from inside the package.
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")

    # The isolated probe the review described: with both records valid and the
    # default virtualized, the default's entry is still surfaced, not only the
    # fallback's.
    assert sorted(_recorded_external_configurations()) == sorted([project_a, project_b])

    bound = bound_project(monkeypatch)
    claude_settings(["Bash(curl *)"])

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["agent_write_restriction"]["ok"] is True, result["steps"]
    # The virtualized default is untouched, its entry migrated rather than moved.
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": [str(project_a)]}
    # The fallback holds all three: its own prior entry, the default's, and the new one.
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(project_a), str(project_b), str(bound)])}
    assert _external_project_record_path() == fallback_record
    assert sorted(_recorded_external_configurations()) == sorted([project_a, project_b, bound])


def test_uninstall_leaves_the_virtualized_default_record_it_cannot_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uninstall must finish after the migration, not crash on what it left (finding 3).

    The migration leaves the virtualized default in place and writes a safe
    fallback beside it. `secure_remove_file` decides whether the file is there
    with a guarded read, which refuses exactly that record's redirected parent, so
    handing it the default aborted the command after the deny rules were already
    gone. The default is reported under `left_alone` instead, the safe fallback is
    removed, and the deny rules come back cleanly.
    """
    bench(tmp_path, monkeypatch)
    virtual = virtualized_user_config(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    prior = Path.home() / "operator-policy" / "legacy" / "config.yaml"
    default_record.parent.mkdir(parents=True, exist_ok=True)
    default_record.write_text(json.dumps({"configurations": [str(prior)]}) + "\n", encoding="utf-8")
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    # Binds a configuration outside the projects directory, which is the one that
    # needs the record `init` migrates and `uninstall` then takes back.
    bound_project(monkeypatch)
    settings = claude_settings(["Bash(curl *)"])

    assert init_project(agent="claude-code")["ok"] is True
    # The migration ran: the safe fallback holds the record, the default is left.
    assert fallback_record.is_file()
    assert default_record.is_file()

    result = uninstall_agent_integration(["claude-code"])

    assert result["ok"] is True, result
    assert deny_rules(settings) == ["Bash(curl *)"]
    # The safe fallback is taken back; the virtualized default is reported and left.
    record_removed = [item["path"] for item in result["removed"] if item["what"] == "project record"]
    record_left = [item for item in result["left_alone"] if item["what"] == "project record"]
    assert record_removed == [str(fallback_record)]
    assert [item["path"] for item in record_left] == [str(default_record)]
    assert not fallback_record.exists()
    assert default_record.exists()
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": [str(prior)]}


# ---------------------------------------------------------------------------
# Review round 3: the reverse of the migration must keep every project, and a
# record removal the filesystem refuses is a failure, not a virtualization
# leftover.


def test_a_migrated_record_survives_the_return_to_an_unpackaged_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse migration must not drop a project's rule (round 3, finding 1).

    A default record an unpackaged host left, virtualized under a package, has a
    new project's entry migrated into the fallback beside it while the default
    stays unsafe and in place. The moment an unpackaged host reads the same profile
    the default is writable again, and a reader that stopped at the first safe
    record surfaced the stale default alone, so every project only the fallback
    named dropped out of the wanted set and the next refresh took its rule back.
    The union across both records keeps them, and the refresh converges the two
    onto the winner.
    """
    bench(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    namespace = Path.home() / ".agentic-hil"

    # Two externally bound projects, both readable so the wanted set is fully
    # established rather than falling back to "remove nothing", with state roots
    # under this tool's own namespace so their rules are ones a refresh could take
    # back as abandoned.
    project_a = Path.home() / "operator-policy" / "alpha" / "config.yaml"
    project_b = Path.home() / "operator-policy" / "beta" / "config.yaml"
    state_a, state_b = namespace / "state-alpha", namespace / "state-beta"
    for config, state in ((project_a, state_a), (project_b, state_b)):
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"state_root: {state.as_posix()!r}\n", encoding="utf-8")

    # The post-migration divergence an unpackaged host now reads: the stale default
    # names only A, the fallback beside it names both. Nothing is virtualized, so
    # the default is a safe write target again.
    default_record.parent.mkdir(parents=True, exist_ok=True)
    default_record.write_text(json.dumps({"configurations": [str(project_a)]}) + "\n", encoding="utf-8")
    fallback_record.parent.mkdir(parents=True, exist_ok=True)
    fallback_record.write_text(json.dumps({"configurations": sorted([str(project_a), str(project_b)])}) + "\n", encoding="utf-8")

    # B's deny rule is standing, alongside the operator's own.
    b_rules = [f"Edit({pattern})" for pattern in _claude_code_deny_patterns(project_b, state_b)]
    settings = claude_settings(["Bash(curl *)", *b_rules])

    # The reader retains both projects even though the default, safe again, names
    # only A.
    assert _external_project_record_path() == default_record
    assert sorted(_recorded_external_configurations()) == sorted([project_a, project_b])

    # A refresh for A alone: B is in the wanted set only through the record union,
    # so this is exactly the refresh that took B's rule back before.
    refresh = restrict_agent_write_access("claude-code", project_a, state_a)

    assert refresh["ok"] is True, refresh
    assert refresh["removed"] == []
    # B's rule is still standing: the union kept its tree wanted.
    assert set(b_rules) <= set(deny_rules(settings))
    # And the two records converged onto the winner rather than staying split, so a
    # later loss of the fallback would no longer take B with it.
    assert json.loads(default_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(project_a), str(project_b)])}
    assert json.loads(fallback_record.read_text(encoding="utf-8")) == {"configurations": sorted([str(project_a), str(project_b)])}


def test_uninstall_reports_a_record_removal_it_could_not_do_as_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real removal failure is not a virtualization leftover (round 3, finding 2).

    A record whose parent resolves to itself is handed to `secure_remove_file`, and
    a removal it turns down there, a read-only mount, an ACL, an I/O error, is a
    genuine failure, not the redirected-parent case `_LEFT_UNSAFE_RECORD` explains.
    It is reported under `failed` with the actual error, the other record is still
    taken back, and the step and the whole command are unsuccessful, because a file
    this installation wrote is still on disk.
    """
    from agentic_hil import cli as cli_module

    bench(tmp_path, monkeypatch)
    default_record, fallback_record = external_project_records()
    bound = Path.home() / "benches" / "alpha" / "config.yaml"
    state = Path.home() / ".agentic-hil" / "state"
    bound.parent.mkdir(parents=True)
    bound.write_text(f"state_root: {state.as_posix()!r}\n", encoding="utf-8")
    for record in (default_record, fallback_record):
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"configurations": [str(bound)]}) + "\n", encoding="utf-8")
    settings = claude_settings(["Bash(curl *)"])
    assert restrict_agent_write_access("claude-code", bound, state)["ok"] is True

    # The default record's removal is refused by the filesystem; the fallback's is
    # not. Nothing is virtualized, so both records resolve stably and both reach
    # `secure_remove_file` rather than the unstable-parent branch before it.
    real_remove = cli_module.secure_remove_file
    refused = os.path.normcase(str(absolute_without_symlinks(default_record)))

    def remove(path: str | Path) -> None:
        if os.path.normcase(str(absolute_without_symlinks(Path(path)))) == refused:
            raise PermissionError("read-only filesystem")
        real_remove(path)

    monkeypatch.setattr("agentic_hil.cli.secure_remove_file", remove)

    result = uninstall_agent_integration(["claude-code"])

    # The step and the whole command are unsuccessful, and the deny rules still
    # came back, the removal order takes them first, before the records.
    assert result["ok"] is False, result
    assert deny_rules(settings) == ["Bash(curl *)"]
    step = result["agents"][0]["steps"]["agent_write_restriction"]
    assert step["ok"] is False
    assert step["error_type"] == "agent_project_record_unremovable"
    # The unremovable record is reported as a failure with the real reason, not as
    # a virtualization leftover under `left_alone`.
    failed = [item for item in result["failed"] if item["what"] == "project record"]
    assert [item["path"] for item in failed] == [str(default_record)]
    assert "read-only filesystem" in failed[0]["error"]
    assert [item for item in result["left_alone"] if item["what"] == "project record"] == []
    # The other record was still attempted and taken back, and the failed one is
    # still on disk.
    record_removed = [item["path"] for item in result["removed"] if item["what"] == "project record"]
    assert record_removed == [str(fallback_record)]
    assert not fallback_record.exists()
    assert default_record.exists()


# ---------------------------------------------------------------------------
# #361: one contract for a missing file under a parent the enforcer refuses.


def redirected_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that is there, opens and reads, and resolves somewhere else.

    The primitive under the profile, without an `init` around it: the checks in
    this section are about what one guarded read answers, so the tree is built
    directly and only `resolve` is moved, exactly as the container moves it.
    """
    virtual = tmp_path / "virtual-appdata"
    holder = virtual / "agentic-hil"
    holder.mkdir(parents=True)
    virtualize(monkeypatch, virtual, tmp_path / "package-roaming-cache")
    return holder


def test_a_missing_file_under_a_redirected_parent_is_refused_not_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract, stated on the case that used to answer per platform.

    The file is not there, and "not there" is not something this spelling can
    establish: the name resolves into another tree, where the file may well be,
    and every write through this spelling is refused anyway. So the read refuses
    with both spellings rather than reporting an absence the tree never asserted.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    missing = holder / "config.yaml"

    with pytest.raises(ConfigError) as refusal:
        secure_optional_read_bytes(missing)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(missing)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_a_present_file_under_a_redirected_parent_is_refused_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same contract, and why absence could not be the one.

    A file that is there was already refused on Windows, and answering absence
    uniformly would have had to drop that refusal, on the platform the redirection
    is real on, or leave one parent refusing a present file and calling a missing
    one absent. The refusal is the answer both cases can share.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    present = holder / "config.yaml"
    present.write_bytes(b"version: 3\n")

    with pytest.raises(ConfigError) as refusal:
        secure_optional_read_bytes(present)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_the_answer_is_settled_before_the_platform_branch_is_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the two platforms cannot drift apart here again.

    `safe_open_binary` is where the split lives: its Windows half consults
    `safe_file_path` before it looks for the file and its POSIX half never does, so
    a contract enforced inside it is a contract each platform states for itself.
    Deciding above it is what makes the answer one answer, and this is the check
    that says so without needing the other platform to run it: the guarded open is
    never reached at all.
    """
    from agentic_hil import config as config_module

    holder = redirected_directory(tmp_path, monkeypatch)
    reached: list[Path] = []

    def recording_open(file_path: str | Path, **kwargs: object) -> None:
        reached.append(Path(file_path))
        raise AssertionError("the guarded open was reached, so the answer is still the platform's")

    monkeypatch.setattr(config_module, "safe_open_binary", recording_open)

    for candidate, note in ((holder / "missing.yaml", "absent"), (holder / "present.yaml", "there")):
        if note == "there":
            candidate.write_bytes(b"version: 3\n")
        with pytest.raises(ConfigError) as refusal:
            secure_optional_read_bytes(candidate)
        assert refusal.value.error_type == "unsafe_configured_path", note

    assert reached == []


def test_the_decoded_sibling_inherits_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every optional read is the same read, so there is one place to pin.

    `secure_optional_read_text` is `secure_optional_read_bytes` decoded, and the
    writers, the removal and the sidecar lock all reach the same function, so none
    of them carries a copy of this rule to drift.
    """
    holder = redirected_directory(tmp_path, monkeypatch)

    with pytest.raises(ConfigError) as refusal:
        secure_optional_read_text(holder / "config.yaml")

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_an_absent_file_under_an_ordinary_parent_is_still_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Holds both ways: the optional read is still optional.

    The refusal is about redirection and nothing else. A file that is simply not
    there, under a directory that names the place it resolves to, is the answer
    this function exists to give and is unchanged.
    """
    ordinary = tmp_path / "ordinary" / "agentic-hil"
    ordinary.mkdir(parents=True)

    assert secure_optional_read_bytes(ordinary / "config.yaml") is None
    assert secure_optional_read_text(ordinary / "config.yaml") is None
    (ordinary / "config.yaml").write_bytes(b"version: 3\n")
    assert secure_optional_read_bytes(ordinary / "config.yaml") == b"version: 3\n"


def test_a_redirected_config_root_is_not_reported_as_an_unconfigured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller this contract is for, and the sentence it stops.

    `config_document_snapshot` separates "the file is not there" from every
    failure, and `parse_config_document` turns the first into "this workspace has
    no Agentic HIL configuration to change". An absence answered for a tree that
    resolves elsewhere arrived as that sentence, which describes an empty profile
    and sends the reader to create a configuration, while what is actually there
    is a root whose every write the enforcer refuses. The refusal carries the
    resolved spelling instead, which is the one line that ends the search.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    target = holder / "config.yaml"

    raw, failure = config_document_snapshot(target)

    assert raw is None
    assert isinstance(failure, ConfigError)
    assert failure.error_type == "unsafe_configured_path"

    with pytest.raises(ConfigError) as refusal:
        load_config_document(target)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


# ---------------------------------------------------------------------------
# #370: the same contract at the raw entry points underneath the secure_* surface.
#
# `secure_optional_read_bytes` was made uniform by #361 and every write through
# `secure_atomic_write_*` goes through it, so the whole trusted-user-file surface
# already answered one way. Two platform splits stayed live one level down, where
# the bench's own state, leases, audit ledger, run records and firmware artifacts
# are read and written: `safe_open_binary`, which every mandatory read goes
# through, and `atomic_write_bytes`, which every raw write goes through. Both had
# a Windows half that consults `safe_file_path` and a POSIX half that consulted
# nothing like it.


def test_the_mandatory_read_of_a_present_file_is_refused_under_a_redirected_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mandatory read is where the bench's own state is read from.

    `safe_read_bytes` is not the optional read: nothing above it turns a refusal
    into an absence, and its callers are the lease records, the audit ledger, the
    run state and the configuration itself. A read that returned bytes on one
    platform and refused on the other had those callers describing the same bench
    two ways, and the bytes are the wrong half to keep: they come out of a tree
    whose every write through this spelling is refused, so nothing that acts on
    them can write the result back.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    present = holder / "lease.json"
    present.write_bytes(b'{"version": 3}\n')

    with pytest.raises(ConfigError) as refusal:
        safe_read_bytes(present)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(present)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_the_mandatory_read_of_a_missing_file_is_refused_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence is the answer this spelling cannot establish, here as well.

    A missing file raises `FileNotFoundError` here, and several callers read that
    as "no lease", "no run", "no report". Under a redirected parent the file may
    well be in the tree the name resolves into, so the refusal is what both cases
    share, exactly as #361 settled it one level up.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    missing = holder / "lease.json"

    with pytest.raises(ConfigError) as refusal:
        safe_read_bytes(missing)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_the_decoded_mandatory_read_inherits_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`safe_read_text` is `safe_read_bytes` decoded, so there is one place to pin."""
    holder = redirected_directory(tmp_path, monkeypatch)
    (holder / "run.json").write_text('{"version": 3}\n', encoding="utf-8")

    with pytest.raises(ConfigError) as refusal:
        safe_read_text(holder / "run.json")

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")


def test_a_raw_write_under_a_redirected_parent_is_refused_and_lands_nowhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that can least afford to differ.

    A raw write that lands is a caller told its bytes are at the spelling it
    named. They are not: they are in the tree that spelling resolves into, and
    every read back through the same name is refused. So the write is refused,
    and the proof is that neither spelling holds a file afterwards, the named one
    because the write never happened and the resolved one because nothing was
    written there either.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    target = holder / "lease.json"

    with pytest.raises(ConfigError) as refusal:
        atomic_write_bytes(target, b'{"version": 3}\n')

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(target)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")
    assert not target.exists()
    assert not (tmp_path / "package-roaming-cache").exists()


def test_the_text_raw_write_inherits_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`atomic_write_text` is `atomic_write_bytes` encoded, so it cannot drift."""
    holder = redirected_directory(tmp_path, monkeypatch)
    target = holder / "report-state.json"

    with pytest.raises(ConfigError) as refusal:
        atomic_write_text(target, '{"version": 3}\n')

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")
    assert not target.exists()


def test_neither_platform_branch_is_reached_by_the_read_or_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why these two cannot drift apart again, said without needing both platforms.

    The split is the platform branch itself: one side walks POSIX descriptors and
    the other holds Windows handles and consults `safe_file_path`, so a contract
    enforced inside either is a contract that platform states for itself. Both
    branch entry points are made to fail loudly here, and the refusal still
    arrives, on whichever platform is running this. That is the same proof #361
    made one level up, and it is what says the answer is settled above the split
    rather than agreed by two branches that happen to match today.
    """
    from agentic_hil import config as config_module

    holder = redirected_directory(tmp_path, monkeypatch)
    (holder / "present.json").write_bytes(b'{"version": 3}\n')
    reached: list[str] = []

    def posix_branch(directory: Path, *, create: bool = False) -> int:
        reached.append(f"posix:{directory}")
        raise AssertionError("the POSIX branch was reached, so the answer is still the platform's")

    def windows_branch(directory: Path, *, create: bool = False) -> list[int]:
        reached.append(f"windows:{directory}")
        raise AssertionError("the Windows branch was reached, so the answer is still the platform's")

    monkeypatch.setattr(config_module, "_open_directory_fd", posix_branch)
    monkeypatch.setattr(config_module, "_windows_hold_directory_chain", windows_branch)

    for candidate in (holder / "present.json", holder / "missing.json"):
        with pytest.raises(ConfigError) as read_refusal:
            safe_read_bytes(candidate)
        assert read_refusal.value.error_type == "unsafe_configured_path", candidate

    with pytest.raises(ConfigError) as write_refusal:
        atomic_write_bytes(holder / "written.json", b"{}\n")
    assert write_refusal.value.error_type == "unsafe_configured_path"

    assert reached == []


def test_an_ordinary_parent_still_reads_and_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Holds both ways: the rule is about redirection and nothing else.

    Every lease, audit line, run record and report on a bench that is not
    redirected goes through exactly these two functions, so this is the check
    that says the refusal was aimed at the profile it names.
    """
    ordinary = tmp_path / "ordinary" / "agentic-hil"
    ordinary.mkdir(parents=True)
    target = ordinary / "lease.json"

    atomic_write_bytes(target, b'{"version": 3}\n')
    assert safe_read_bytes(target) == b'{"version": 3}\n'

    atomic_write_text(target, '{"version": 4}\n')
    assert safe_read_text(target) == '{"version": 4}\n'

    with pytest.raises(FileNotFoundError):
        safe_read_bytes(ordinary / "absent.json")


def test_the_component_walk_still_answers_for_a_chain_that_resolves_to_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule was added above the walk, not in place of it.

    Hoisting `safe_file_path` into the POSIX branch would have looked like the
    same fix and would have replaced this: its object check answers a bad chain
    with one sentence about an output file, where the walk underneath names the
    component that stopped it. A regular file standing where a directory belongs
    resolves to the spelling it was named by, so the redirect rule says nothing
    about it and the platform walk is what answers, which is the whole point of
    stating the two separately.
    """
    base = tmp_path / "ordinary"
    base.mkdir()
    (base / "not-a-directory").write_bytes(b"contents\n")
    blocked = base / "not-a-directory" / "deeper" / "lease.json"

    with pytest.raises(ConfigError) as refusal:
        safe_read_bytes(blocked)

    assert refusal.value.error_type == "unsafe_configured_path"
    # Not the redirect refusal: this chain names the place it resolves to, and
    # answering it with `resolved_parent` would send the reader after a
    # redirection that is not there.
    assert "resolved_parent" not in refusal.value.details

    with pytest.raises(ConfigError) as write_refusal:
        atomic_write_bytes(blocked, b"{}\n")

    assert write_refusal.value.error_type == "unsafe_configured_path"
    assert "resolved_parent" not in write_refusal.value.details


def test_leaving_the_workspace_is_still_answered_before_redirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Artifact validation reaches these two with `workspace=`, and it goes first.

    A path outside the configured workspace is refused for that, whatever its
    parent resolves to. No permission relaxes workspace containment, so a caller
    told about a redirection would go and repair a tree it may not use in any
    case.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (holder / "firmware.elf").write_bytes(b"\x7fELF")

    with pytest.raises(ConfigError) as refusal:
        safe_read_bytes(holder / "firmware.elf", workspace=workspace)

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.summary == "Path leaves the workspace."
    assert "resolved_parent" not in refusal.value.details


# ---------------------------------------------------------------------------
# #370 again: the two raw primitives that open their named file directly rather
# than through `safe_open_binary` or `atomic_write_bytes`. `safe_file_lock`
# creates the lock the canonical audit ledger and the recovery ledger serialize
# on, and `safe_append_text` writes the audit line itself, so a redirected parent
# these two answered per platform was the same incomplete contract one function
# further out: their Windows halves reach `safe_file_path` and their POSIX halves
# open the named file directly, so a lock taken or a line appended under such a
# parent landed on POSIX and was refused on Windows.


def test_the_lock_primitive_is_refused_under_a_redirected_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock the audit and recovery ledgers serialize on cannot be per-platform.

    A lock file created under a parent that names one place and resolves to
    another is a lock two writers on the two platforms would take in two different
    trees, so the serialization the ledger depends on is exactly what the
    redirection breaks. It is refused, and the proof is that neither spelling holds
    the lock afterwards.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    target = holder / "audit.lock"

    with pytest.raises(ConfigError) as refusal, safe_file_lock(target):
        pass

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(target)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")
    assert not target.exists()
    assert not (tmp_path / "package-roaming-cache").exists()


def test_the_append_primitive_is_refused_under_a_redirected_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit line itself is a write, and a write cannot differ across platforms.

    An audit line appended under a redirected parent goes into the tree the name
    resolves into, while every read of the ledger through the spelling that wrote
    it is refused, so the record and the reader describe two different files. It is
    refused before either branch, and neither spelling holds a ledger afterwards.
    """
    holder = redirected_directory(tmp_path, monkeypatch)
    target = holder / "audit.ndjson"

    with pytest.raises(ConfigError) as refusal:
        safe_append_text(target, '{"event": "probe"}\n')

    assert refusal.value.error_type == "unsafe_configured_path"
    assert refusal.value.details["path"] == str(target)
    assert refusal.value.details["resolved_parent"] == str(tmp_path / "package-roaming-cache" / "agentic-hil")
    assert not target.exists()
    assert not (tmp_path / "package-roaming-cache").exists()


def test_neither_platform_branch_is_reached_by_the_lock_or_the_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why these two cannot drift apart again, said without needing both platforms.

    The split is the platform branch itself: one side walks POSIX descriptors from
    `_open_directory_fd` and the other holds Windows handles from
    `_windows_hold_directory_chain` and consults `safe_file_path`, so a contract
    enforced inside either is a contract that platform states for itself. Both
    branch entry points are made to fail loudly here, and the refusal still
    arrives, on whichever platform is running this, which says the answer is
    settled above the split rather than agreed by two branches that match today.
    """
    from agentic_hil import config as config_module

    holder = redirected_directory(tmp_path, monkeypatch)
    reached: list[str] = []

    def posix_branch(directory: Path, *, create: bool = False) -> int:
        reached.append(f"posix:{directory}")
        raise AssertionError("the POSIX branch was reached, so the answer is still the platform's")

    def windows_branch(directory: Path, *, create: bool = False) -> list[int]:
        reached.append(f"windows:{directory}")
        raise AssertionError("the Windows branch was reached, so the answer is still the platform's")

    monkeypatch.setattr(config_module, "_open_directory_fd", posix_branch)
    monkeypatch.setattr(config_module, "_windows_hold_directory_chain", windows_branch)

    with pytest.raises(ConfigError) as lock_refusal, safe_file_lock(holder / "audit.lock"):
        pass
    assert lock_refusal.value.error_type == "unsafe_configured_path"

    with pytest.raises(ConfigError) as append_refusal:
        safe_append_text(holder / "audit.ndjson", "line\n")
    assert append_refusal.value.error_type == "unsafe_configured_path"

    assert reached == []


def test_the_lock_and_append_still_serve_an_ordinary_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Holds both ways: the rule is about redirection and nothing else.

    Every lock the ledgers take and every audit line a bench that is not
    redirected writes goes through exactly these two functions, so this is the
    check that says the refusal was aimed at the profile it names and left the
    ordinary bench alone.
    """
    ordinary = tmp_path / "ordinary" / "agentic-hil"
    ordinary.mkdir(parents=True)

    lock_target = ordinary / "audit.lock"
    with safe_file_lock(lock_target):
        pass
    assert lock_target.exists()

    ledger = ordinary / "audit.ndjson"
    safe_append_text(ledger, "one\n")
    safe_append_text(ledger, "two\n")
    assert ledger.read_text(encoding="utf-8") == "one\ntwo\n"
