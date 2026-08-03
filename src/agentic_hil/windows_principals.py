"""Who actually holds rights on a rejected Windows path.

The ancestor trust check is right to refuse ``%APPDATA%`` on a profile where an
app-capability ACE grants FullControl on ``AppData``: the right is real, not a
false positive. What the refusal could not say was *whose* right it is. An
operator reading ``S-1-15-3-3557520199-...`` cannot tell whether to move the
configuration or to revoke that grant, and that is a decision only a person can
make.

A capability SID ``S-1-15-3-<sub-authorities>`` shares its sub-authorities with
the package SID ``S-1-15-2-<same sub-authorities>``, and Windows registers that
package SID in two places a normal user may read:

* ``HKLM\\SOFTWARE\\Microsoft\\SecurityManager\\CapAuthz\\ApplicationsEx\\<PackageFullName>``
  carries the package SID in its ``PackageSid`` value, so the key name is the
  full package name, e.g. ``Claude_1.24012.9.0_x64__pzs8sxrjxfjjc``.
* ``HKEY_CLASSES_ROOT\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\AppContainer\\Mappings\\<package SID>``
  carries ``Moniker`` (the package family name) and ``DisplayName``.

Resolution is best effort by construction. Every lookup here fails soft: an
unresolvable SID is reported as the SID and nothing else, and never as an error.
A refusal that could not name a package is still a correct refusal, while a
refusal that raised while trying to be helpful would be a new failure mode on
top of the one being reported.

Nothing in this module runs off Windows. ``winreg`` and ``ctypes.wintypes`` are
imported inside the functions, so importing this module on POSIX costs an import
of ``os`` and nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from contextlib import suppress

from agentic_hil.types import JsonObject

CAPABILITY_SID_PREFIX = "S-1-15-3-"
PACKAGE_SID_PREFIX = "S-1-15-2-"

_CAP_AUTHZ_APPLICATIONS = r"SOFTWARE\Microsoft\SecurityManager\CapAuthz\ApplicationsEx"
_APPCONTAINER_MAPPINGS = r"Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"

# One measured scan of ApplicationsEx costs about 90 ms on a profile with 176
# registered packages. That is paid only on a path that is already being
# refused, never on a healthy load, and only until a package matches.
_MAX_SCANNED_PACKAGES = 4096


def _on_windows() -> bool:
    """The single gate on every Windows-only lookup in this module.

    One named predicate rather than an inline check per function, so a test can
    prove that nothing below it is reached off Windows instead of asserting the
    absence of an effect.
    """
    return os.name == "nt"


def package_sid_for_capability(sid: str) -> str | None:
    """The package SID that shares this capability SID's sub-authorities."""
    if not sid.startswith(CAPABILITY_SID_PREFIX):
        return None
    return PACKAGE_SID_PREFIX + sid[len(CAPABILITY_SID_PREFIX) :]


def describe_principal(sid: str) -> JsonObject:
    """What is known about one SID: always its ``sid``, more when it resolves.

    Adds ``account`` for a SID the local security authority can name, and for an
    app-capability SID ``package``, ``package_family``, ``display_name`` and
    ``package_sid`` when the registry answers. Never raises.
    """
    described: JsonObject = {"sid": sid}
    if not sid or not _on_windows():
        return described
    try:
        # Broad on purpose: naming the holder is a courtesy, refusing the path is
        # the duty, and the courtesy must not be able to take the duty down.
        _resolve_into(sid, described)
    except Exception:
        return {"sid": sid}
    return described


def _resolve_into(sid: str, described: JsonObject) -> None:
    account = _account_name(sid)
    if account:
        described["account"] = account
    package_sid = package_sid_for_capability(sid)
    if package_sid is None:
        return
    described["kind"] = "app_capability"
    described["package_sid"] = package_sid
    mapping = _appcontainer_mapping(package_sid)
    if mapping.get("Moniker"):
        described["package_family"] = mapping["Moniker"]
    if mapping.get("DisplayName"):
        described["display_name"] = mapping["DisplayName"]
    package = _package_full_name(package_sid)
    if package:
        described["package"] = package


def describe_principals(sids: Iterable[str]) -> list[JsonObject]:
    """Describe every SID once, in the order first seen."""
    seen: list[str] = []
    for sid in sids:
        if sid and sid not in seen:
            seen.append(sid)
    return [describe_principal(sid) for sid in seen]


def principal_label(described: JsonObject) -> str:
    """The shortest honest name for a described principal."""
    package = described.get("package") or described.get("package_family")
    display = described.get("display_name")
    sid = str(described.get("sid", ""))
    if package and display:
        return f"package {package} ({display})"
    if package:
        return f"package {package}"
    account = described.get("account")
    if account:
        return f"{account} ({sid})"
    return sid


def untrusted_principal_details(sids: Sequence[str]) -> JsonObject:
    """Refusal fields naming who holds the rights, or nothing when nobody is known.

    Merged into the ``unsafe_configured_path`` details, so the refusal, `doctor`
    and the MCP result all say the same thing without a second code path.
    """
    described = describe_principals(sids)
    if not described:
        return {}
    labels = [principal_label(entry) for entry in described]
    holders = ", ".join(labels)
    return {
        "untrusted_principals": described,
        "untrusted_principals_summary": (
            f"Full control over this directory is held by {holders}. Choose one: put the configuration and "
            "state_root in a permitted location, which needs no privileges and changes nothing on the system, "
            "or have the operator remove that grant through the application that made it. Do not edit the ACL "
            "to make the check pass."
        ),
    }


def _account_name(sid: str) -> str | None:
    """``DOMAIN\\name`` for a SID the local security authority can name.

    App-capability SIDs have no account name — LookupAccountSid fails on them,
    which is exactly why the registry route below exists — so this stays quiet
    rather than reporting the failure.
    """
    import ctypes
    from ctypes import wintypes

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi32.ConvertStringSidToSidW
        convert.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        convert.restype = wintypes.BOOL
        lookup = advapi32.LookupAccountSidW
        lookup.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_int),
        ]
        lookup.restype = wintypes.BOOL
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        binary = ctypes.c_void_p()
        if not convert(sid, ctypes.byref(binary)):
            return None
        try:
            name_size = wintypes.DWORD(256)
            domain_size = wintypes.DWORD(256)
            name = ctypes.create_unicode_buffer(name_size.value)
            domain = ctypes.create_unicode_buffer(domain_size.value)
            use = ctypes.c_int()
            if not lookup(None, binary, name, ctypes.byref(name_size), domain, ctypes.byref(domain_size), ctypes.byref(use)):
                return None
            if not name.value:
                return None
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            if binary:
                local_free(binary)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return None


def _appcontainer_mapping(package_sid: str) -> dict[str, str]:
    """``Moniker`` and ``DisplayName`` for a package SID, or an empty mapping."""
    values: dict[str, str] = {}
    import winreg

    with suppress(OSError, ValueError), winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{_APPCONTAINER_MAPPINGS}\\{package_sid}") as key:
        for name in ("Moniker", "DisplayName"):
            with suppress(OSError, ValueError):
                value, kind = winreg.QueryValueEx(key, name)
                if kind == winreg.REG_SZ and isinstance(value, str) and value:
                    values[name] = value
    return values


def _package_full_name(package_sid: str) -> str | None:
    """The full package name registered against this package SID.

    ``ApplicationsEx`` is keyed by package full name and holds the SID as a
    value, so the only way from SID to name is a scan. It is bounded, read-only,
    and reached only when a path has already been refused.
    """
    import winreg

    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CAP_AUTHZ_APPLICATIONS, 0, winreg.KEY_READ)
    except OSError:
        return None
    try:
        for index in range(_MAX_SCANNED_PACKAGES):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                return None
            with suppress(OSError, ValueError), winreg.OpenKey(root, name) as package_key:
                value, kind = winreg.QueryValueEx(package_key, "PackageSid")
                if kind == winreg.REG_SZ and isinstance(value, str) and value.upper() == package_sid.upper():
                    return name
    finally:
        root.Close()
    return None
