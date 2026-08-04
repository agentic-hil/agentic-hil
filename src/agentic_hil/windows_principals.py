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

# What a principal is, which is what decides whether holding a right on a path is
# a reason to refuse it. Measured on Windows 11 26200, see PLATFORM_PATHS.
#
# ``account``    a SID the local security authority resolves to a real account or
#                group. Somebody can log on as it, or be a member of it, and
#                therefore actually exercise the right.
# ``app_package``an application-package or app-capability identity
#                (``S-1-15-2-*`` / ``S-1-15-3-*``). It is a facet of software this
#                user installed and it runs in an AppContainer, i.e. with a
#                *subset* of the user's own rights. Any ordinary unpackaged
#                process of the same user already has full access to everything
#                the user owns, so such an ACE grants no reach that was not
#                already there.
# ``unresolved`` a SID the local security authority *answered about*, saying that
#                nothing on this machine maps to it (``ERROR_NONE_MAPPED``). No
#                token built here carries it. It is normally residue of software
#                that wrote an ACE with a SID from another machine's image, and
#                it is present on a stock Windows 11 ``%LOCALAPPDATA%``.
# ``lookup_failed`` the question could not be asked: the SID would not convert,
#                the authority was unreachable, the call raised. This is *not*
#                the same as ``unresolved`` and must never be folded into it. A
#                live foreign account whose lookup happened to fail would be
#                reported as an orphan SID, and a tolerated class would then be
#                reached by breaking the lookup rather than by being benign.
PRINCIPAL_CLASS_ACCOUNT = "account"
PRINCIPAL_CLASS_APP_PACKAGE = "app_package"
PRINCIPAL_CLASS_UNRESOLVED = "unresolved"
PRINCIPAL_CLASS_LOOKUP_FAILED = "lookup_failed"

# LookupAccountSid's way of saying "I looked, and there is nothing". Any other
# failure is the lookup itself failing and means the opposite: nothing was
# established either way.
_ERROR_NONE_MAPPED = 1332

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


def principal_class(sid: str) -> str:
    """Which of the four kinds of principal this SID is. Never raises.

    Structure decides first, because an application-package identity is
    recognisable from the SID alone and needs no lookup. Everything else is the
    local security authority's answer — and *whether it answered* is part of that
    answer. "There is no such account" is a fact about the SID; "I could not
    ask" is a fact about this moment, and the two used to collapse into one
    class. They must not: `unresolved` is tolerated under the default trust mode
    because a stock profile really does carry orphan SIDs, so folding a failed
    lookup into it would turn every transient LSA failure into a pass for
    whatever account the ACE actually names.
    """
    if not sid:
        # An ACE whose SID would not even stringify. The right is real and its
        # holder is unknown, which is ignorance rather than an absent account.
        return PRINCIPAL_CLASS_LOOKUP_FAILED
    if sid.startswith(CAPABILITY_SID_PREFIX) or sid.startswith(PACKAGE_SID_PREFIX):
        return PRINCIPAL_CLASS_APP_PACKAGE
    if not _on_windows():
        return PRINCIPAL_CLASS_LOOKUP_FAILED
    try:
        outcome, _ = lookup_account(sid)
    except Exception:  # pragma: no cover - lookup_account catches its own failures
        return PRINCIPAL_CLASS_LOOKUP_FAILED
    return outcome


def describe_principal(sid: str) -> JsonObject:
    """What is known about one SID: always its ``sid``, more when it resolves.

    Adds ``principal_class``, ``account`` for a SID the local security authority
    can name, and for an app-capability SID ``package``, ``package_family``,
    ``display_name`` and ``package_sid`` when the registry answers. Never raises.
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
    outcome, account = lookup_account(sid)
    if account:
        described["account"] = account
    package_sid = package_sid_for_capability(sid)
    described["principal_class"] = (
        PRINCIPAL_CLASS_APP_PACKAGE if package_sid is not None or sid.startswith(PACKAGE_SID_PREFIX) else outcome
    )
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
    holders = ", ".join(principal_label(entry) for entry in described)
    packaged = any(entry.get("package") or entry.get("package_family") for entry in described)
    revoke = (
        "have the operator remove that grant through the application that holds it"
        if packaged
        else "have the operator remove that grant at its source"
    )
    return {
        "untrusted_principals": described,
        "untrusted_principals_summary": (
            f"The rights that make this path replaceable are held by {holders}. Choose one: put the "
            f"configuration and state_root in a permitted location, which needs no privileges and changes "
            f"nothing on the system, or {revoke}. Do not edit the ACL to make the check pass."
        ),
    }


def lookup_account(sid: str) -> tuple[str, str | None]:
    """What the local security authority says about one SID. Never raises.

    Returns ``(class, account name)``, where the class is one of
    ``PRINCIPAL_CLASS_ACCOUNT``, ``PRINCIPAL_CLASS_UNRESOLVED`` or
    ``PRINCIPAL_CLASS_LOOKUP_FAILED``. The distinction between the last two is
    the whole reason this returns a class rather than an optional name: only
    ``ERROR_NONE_MAPPED`` is the authority saying there is no such account.
    Every other failure — a SID that will not convert, an unreachable authority,
    a raised call — leaves the holder unknown, and unknown has to be able to
    reach a stricter verdict than "orphan SID nobody can log on as".

    App-capability SIDs have no account name; the caller recognises those from
    the SID itself before asking, and the registry route below is what names
    them.
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
            return PRINCIPAL_CLASS_LOOKUP_FAILED, None
        try:
            name_size = wintypes.DWORD(256)
            domain_size = wintypes.DWORD(256)
            name = ctypes.create_unicode_buffer(name_size.value)
            domain = ctypes.create_unicode_buffer(domain_size.value)
            use = ctypes.c_int()
            ctypes.set_last_error(0)
            if not lookup(None, binary, name, ctypes.byref(name_size), domain, ctypes.byref(domain_size), ctypes.byref(use)):
                code = ctypes.get_last_error()
                return (PRINCIPAL_CLASS_UNRESOLVED if code == _ERROR_NONE_MAPPED else PRINCIPAL_CLASS_LOOKUP_FAILED), None
            if not name.value:
                return PRINCIPAL_CLASS_UNRESOLVED, None
            return PRINCIPAL_CLASS_ACCOUNT, (f"{domain.value}\\{name.value}" if domain.value else name.value)
        finally:
            if binary:
                local_free(binary)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return PRINCIPAL_CLASS_LOOKUP_FAILED, None


def _account_name(sid: str) -> str | None:
    """``DOMAIN\\name`` for a SID the local security authority can name."""
    return lookup_account(sid)[1]


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
