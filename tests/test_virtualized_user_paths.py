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

from agentic_hil.config import (
    ConfigError,
    load_authoritative_config,
    provisionable_state_root,
    resolve_stable_directory,
    safe_writable_directory,
    user_state_root,
)
from agentic_hil.tools import (
    PROJECT_CONFIG_CREATE,
    AgenticHILToolService,
    UnprovisionedToolService,
    audit_gated_tools,
    audited_hardware_tools,
)
from tests.test_agent_provisioning import attached_hardware, bench, written_document


def virtualize(monkeypatch: pytest.MonkeyPatch, source: Path, destination: Path) -> None:
    """Make ``source`` resolve onto ``destination``, and nothing else move.

    The stand-in for AppContainer path virtualization. Only ``resolve`` is
    touched, which is exactly the reach the container has: ``lstat``, the reparse
    attributes, the link count and ``samestat`` all keep answering about the
    spelling that was opened, and every other tree on the machine resolves as it
    did. ``destination`` need not exist; the container's backing tree is created
    on first write and the checks under test never look inside it.
    """
    real = Path.resolve
    prefix = os.path.normcase(str(source))

    def resolve(self: Path, strict: bool = False) -> Path:
        resolved = real(self, strict=strict)
        text = os.path.normcase(str(resolved))
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
