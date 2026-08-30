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

import os
from pathlib import Path

import pytest
import yaml

from agentic_hil.bench import BenchMutex
from agentic_hil.cli import _configuration_the_projects_walk_finds, _visible_project_configurations
from agentic_hil.config import (
    ConfigError,
    authoritative_config_target,
    load_authoritative_config,
    project_config_directories,
    project_config_directory,
    project_config_leaf,
    project_config_path,
    provisionable_state_root,
    resolve_stable_directory,
    safe_file_path,
    safe_writable_directory,
    user_state_root,
)
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

    `ensure_audit_ready` refuses two ways and only one — the `state_root` spelling
    the enforcer will not accept — is the one a regeneration replaces. A corrupt
    `report-state.json` under a `state_root` that resolves cleanly is the other:
    `config_invalid`, which the same regeneration leaves exactly in place, because
    it selects the same healthy root and rewrites nothing under it. Reading the
    board around it would bypass the gate for an integrity failure and report a
    repair that never lands — the next `probe_target` would meet the same wall.
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
    debugger can carry it with `probe_id` still null — so the enumerated
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
