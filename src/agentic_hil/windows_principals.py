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
import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress

from agentic_hil.types import JsonObject

CAPABILITY_SID_PREFIX = "S-1-15-3-"
PACKAGE_SID_PREFIX = "S-1-15-2-"
# ``S-1-5-5-<high>-<low>`` names one live logon session. Microsoft documents
# LookupAccountSid as returning ERROR_NONE_MAPPED for a logon SID — it has no
# account name to return — while every access token of that session carries it.
# So it is the exact shape that must never reach the tolerated orphan class: the
# answer is identical to an orphan's and the meaning is the opposite.
LOGON_SESSION_SID_PREFIX = "S-1-5-5-"
# ``S-1-12-1-<four sub-authorities>`` is an Entra ID (Azure AD) principal. On a
# machine that cannot resolve it the lookup answers ERROR_NONE_MAPPED, and the
# identity is still live — it belongs to a directory this machine is not asking.
ENTRA_SID_PREFIX = "S-1-12-1-"

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
#                nothing on this machine maps to it (``ERROR_NONE_MAPPED``), on a
#                machine where that answer is conclusive — see
#                ``none_mapped_is_conclusive`` — *and* whose form is the one that
#                answer can be conclusive about: ``S-1-5-21-<a>-<b>-<c>-<rid>``,
#                an account in some SAM or domain. No token built here carries
#                it. It is normally residue of software that wrote an ACE with a
#                SID from another machine's image, and it is present on a stock
#                Windows 11 ``%LOCALAPPDATA%`` (measured: an ``(OI)(CI)(M)`` ACE
#                for an ``S-1-5-21-…`` SID from the installation image). The form
#                is checked rather than assumed because several SID shapes have no
#                account name *by construction* and return the same code while
#                naming something a live token carries — see
#                ``LOGON_SESSION_SID_PREFIX``.
# ``logon_session`` ``S-1-5-5-<high>-<low>``, recognised from the SID alone. It
#                identifies one live logon session and rides in that session's
#                access tokens, and it has no account name to look up, so
#                ``ERROR_NONE_MAPPED`` is its normal answer rather than evidence
#                of anything. An ACE for another session's logon SID is a right
#                somebody is holding right now.
# ``entra``      ``S-1-12-1-*``, an Entra ID (Azure AD) principal, recognised
#                from the SID alone once the lookup has declined to name it. The
#                directory that could name it is not one this machine is asking,
#                so none-mapped says nothing about whether the holder is live.
# ``unresolved_foreign`` ``ERROR_NONE_MAPPED`` where it establishes nothing:
#                either the machine has somewhere else to ask, or the SID's form
#                is not the account form above. ``LookupAccountSid`` consults
#                trusted domains, and a trust that is unreachable, or a SID this
#                domain controller does not know while another one in the forest
#                does, produces exactly this answer for a SID a live token can
#                still carry — including through SID history, which Windows
#                authorizes on as raw SIDs. So it is *reported as* none-mapped and
#                *treated as* ignorance.
# ``lookup_failed`` the question could not be asked: the SID would not convert,
#                the authority was unreachable, the call raised. This is *not*
#                the same as ``unresolved`` and must never be folded into it. A
#                live foreign account whose lookup happened to fail would be
#                reported as an orphan SID, and a tolerated class would then be
#                reached by breaking the lookup rather than by being benign.
PRINCIPAL_CLASS_ACCOUNT = "account"
PRINCIPAL_CLASS_APP_PACKAGE = "app_package"
PRINCIPAL_CLASS_UNRESOLVED = "unresolved"
PRINCIPAL_CLASS_LOGON_SESSION = "logon_session"
PRINCIPAL_CLASS_ENTRA = "entra"
PRINCIPAL_CLASS_UNRESOLVED_FOREIGN = "unresolved_foreign"
PRINCIPAL_CLASS_LOOKUP_FAILED = "lookup_failed"

# The only SID form ``ERROR_NONE_MAPPED`` can be a clean bill of health about: an
# account or group in some SAM or domain, ``S-1-5-21-<a>-<b>-<c>-<rid>``. If the
# authority that owns that prefix is the local SAM and the local SAM does not
# know it, nothing here can log on as it. Every other form is either well-known
# (and would have resolved), or nameless by construction and therefore says
# nothing when it fails to resolve. Sub-authorities are 32-bit, so ten digits is
# the widest a real one gets; a longer run is not this form.
_ORPHAN_ACCOUNT_SID = re.compile(r"^S-1-5-21-\d{1,10}-\d{1,10}-\d{1,10}-\d{1,10}$", re.IGNORECASE)

# LookupAccountSid's way of saying "I looked, and there is nothing". Any other
# failure is the lookup itself failing and means the opposite: nothing was
# established either way.
_ERROR_NONE_MAPPED = 1332

# NETSETUP_JOIN_STATUS, from lmjoin.h. Only the domain answer changes anything
# here; the rest are grouped as "this machine has nowhere else to ask".
_NET_SETUP_UNKNOWN_STATUS = 0
_NET_SETUP_DOMAIN_NAME = 3

# DSREG_JOIN_TYPE, from lmjoin.h. DSREG_UNKNOWN_JOIN is "not joined at all"; both
# other values (device join, workplace join) mean an Entra tenant is behind this
# machine's identities.
_DSREG_UNKNOWN_JOIN = 0

JOIN_STATE_WORKGROUP = "workgroup"
JOIN_STATE_DOMAIN = "domain_joined"
# Entra (Azure AD) joined or workplace-joined. `NetGetJoinInformation` reports
# such a machine as a workgroup member — the classic API predates Entra and
# describes only the NT domain relationship — so asking it alone would call a
# machine with a whole cloud directory behind it "nowhere else to ask".
JOIN_STATE_ENTRA = "entra_joined"
JOIN_STATE_UNKNOWN = "unknown"

# Fixed for the life of a process: joining or leaving a domain needs a reboot.
# Cached because it is asked once per ACE on a path being validated.
_join_state: str | None = None

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


def machine_join_state() -> str:
    """Whether this machine has an authority beyond its own SAM to ask about a SID.

    ``NetGetJoinInformation`` is a local read of the join configuration — it does
    not go on the network — and it is most of what decides whether
    ``ERROR_NONE_MAPPED`` is evidence or noise. It is not all of it: that API
    describes the classic NT domain relationship only, and answers "workgroup" for
    a machine joined to an Entra tenant, which is an authority in every sense that
    matters here. ``NetGetAadJoinInformation`` is the separate local read that
    covers it, and it is asked whenever the classic answer was "workgroup".

    Never raises; a failure of the classic call is ``unknown``, which is treated
    as the domain case because not knowing where the question went is not a reason
    to believe the answer.
    """
    global _join_state
    if _join_state is not None:
        return _join_state
    if not _on_windows():
        _join_state = JOIN_STATE_WORKGROUP
        return _join_state
    _join_state = _query_join_state()
    return _join_state


def _query_join_state() -> str:
    import ctypes
    from ctypes import wintypes

    try:
        netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
        get_join = netapi32.NetGetJoinInformation
        get_join.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(ctypes.c_int)]
        get_join.restype = wintypes.DWORD
        free_buffer = netapi32.NetApiBufferFree
        free_buffer.argtypes = [ctypes.c_void_p]
        free_buffer.restype = wintypes.DWORD
        name = wintypes.LPWSTR()
        status = ctypes.c_int(_NET_SETUP_UNKNOWN_STATUS)
        if get_join(None, ctypes.byref(name), ctypes.byref(status)) != 0:
            return JOIN_STATE_UNKNOWN
        try:
            return join_state_for(status.value, _entra_joined)
        finally:
            if name:
                free_buffer(ctypes.cast(name, ctypes.c_void_p))
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return JOIN_STATE_UNKNOWN


def join_state_for(status: int, entra_joined: Callable[[], bool]) -> str:
    """The join state the two local reads add up to.

    Separated from the ctypes that produce the inputs so the rule itself can be
    exercised on any platform: this is where "workgroup" stops meaning "nowhere
    else to ask", and a rule that can only be checked by joining a real tenant is
    a rule nothing checks.

    ``entra_joined`` is a callable rather than a value so the second read is only
    performed in the one case whose answer it can change.
    """
    if status == _NET_SETUP_DOMAIN_NAME:
        return JOIN_STATE_DOMAIN
    if status == _NET_SETUP_UNKNOWN_STATUS:
        return JOIN_STATE_UNKNOWN
    return JOIN_STATE_ENTRA if entra_joined() else JOIN_STATE_WORKGROUP


def _entra_joined() -> bool:
    """Whether an Entra tenant stands behind this machine's identities.

    ``NetGetAadJoinInformation`` is local, like the classic join read, and
    ``joinType`` is the first field of ``DSREG_JOIN_INFO`` — the only field this
    question needs, so the rest of the structure is never described to ctypes.

    A call that fails is read as "not joined". That direction is deliberate: this
    query only ever *narrows* what none-mapped may be tolerated for, the tolerated
    class is now restricted to the classic ``S-1-5-21-*`` account form on its own,
    and an Entra principal is ``S-1-12-1-*`` and is classified by its own shape
    whatever this returns. Reading a failure as "joined" would instead refuse the
    documented default ``state_root`` on every machine where the call is
    unavailable, for no gain in what is actually established.
    """
    import ctypes
    from ctypes import wintypes

    try:
        netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
        get_aad = netapi32.NetGetAadJoinInformation
        get_aad.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        get_aad.restype = ctypes.c_long
        free_aad = netapi32.NetFreeAadJoinInformation
        free_aad.argtypes = [ctypes.c_void_p]
        free_aad.restype = None
        info = ctypes.c_void_p()
        if get_aad(None, ctypes.byref(info)) != 0 or not info:
            return False
        try:
            return ctypes.c_int.from_address(info.value).value != _DSREG_UNKNOWN_JOIN
        finally:
            free_aad(info)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return False


def none_mapped_is_conclusive() -> bool:
    """Whether this machine has anywhere but its own SAM to ask about a SID.

    On a machine that is in a workgroup and joined to no Entra tenant, the local
    SAM is the only account database ``LookupAccountSid`` consults and it is
    always reachable, so "no such account" can be a fact about the SID. On a
    domain member the same call also asks the domain and every trust behind it,
    and Microsoft documents ``ERROR_NONE_MAPPED`` as the result when that question
    cannot be answered — an unreachable trust, a controller that does not hold the
    object. A live account's SID, or a SID carried only in SID history, then
    arrives looking exactly like an orphan from an installation image.

    This is a necessary condition and not a sufficient one, which is why it is
    named for what it measures rather than for the verdict. Even where nothing
    else was asked, ``ERROR_NONE_MAPPED`` is only a clean bill for a SID whose
    form *has* an account name to be missing; ``_none_mapped_class`` applies both
    halves.
    """
    return machine_join_state() == JOIN_STATE_WORKGROUP


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
    ``PRINCIPAL_CLASS_ACCOUNT``, ``PRINCIPAL_CLASS_UNRESOLVED``,
    ``PRINCIPAL_CLASS_LOGON_SESSION``, ``PRINCIPAL_CLASS_ENTRA``,
    ``PRINCIPAL_CLASS_UNRESOLVED_FOREIGN`` or ``PRINCIPAL_CLASS_LOOKUP_FAILED``.
    The distinctions are the whole reason this returns a class rather than an
    optional name. Only ``ERROR_NONE_MAPPED`` is the authority saying there is no
    such account; every other failure — a SID that will not convert, an
    unreachable authority, a raised call — leaves the holder unknown, and unknown
    has to be able to reach a stricter verdict than "orphan SID nobody can log on
    as". And ``ERROR_NONE_MAPPED`` itself is that answer only where the local SAM
    was the only thing asked *and* the SID is of a form that has an account name
    to be missing; a logon-session or Entra SID returns the same code while naming
    something live. ``_none_mapped_class`` splits the cases apart rather than
    letting any of them inherit the tolerated one.

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
                return (_none_mapped_class(sid) if code == _ERROR_NONE_MAPPED else PRINCIPAL_CLASS_LOOKUP_FAILED), None
            if not name.value:
                return _none_mapped_class(sid), None
            return PRINCIPAL_CLASS_ACCOUNT, (f"{domain.value}\\{name.value}" if domain.value else name.value)
        finally:
            if binary:
                local_free(binary)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return PRINCIPAL_CLASS_LOOKUP_FAILED, None


def _none_mapped_class(sid: str) -> str:
    """Which class ``ERROR_NONE_MAPPED`` is, for this SID on this machine.

    Two independent things have to hold before the answer is the tolerated one,
    and the join state was only ever the first of them.

    The second is the SID's own form. ``ERROR_NONE_MAPPED`` means "no account name
    for this", and several SID shapes have no account name *by construction* while
    naming something an access token carries right now. A logon session SID
    ``S-1-5-5-<high>-<low>`` is the sharp case: it is documented as returning that
    code, it identifies a live logon session, and it rides in that session's
    tokens — so an ACE granting it write on an ancestor is a right another session
    is holding, arriving through the same return value as an orphan from an
    installation image. An Entra principal ``S-1-12-1-*`` is the same story with a
    directory instead of a session behind it.

    So the tolerance is granted positively, to the one form the answer can be
    conclusive about — ``S-1-5-21-<a>-<b>-<c>-<rid>``, an account in some SAM,
    which is what a stock ``%LOCALAPPDATA%`` actually carries — and everything
    else falls to the ignorance class it always belonged in.
    """
    upper = sid.upper()
    if upper.startswith(LOGON_SESSION_SID_PREFIX):
        return PRINCIPAL_CLASS_LOGON_SESSION
    if upper.startswith(ENTRA_SID_PREFIX):
        return PRINCIPAL_CLASS_ENTRA
    if none_mapped_is_conclusive() and _ORPHAN_ACCOUNT_SID.match(sid):
        return PRINCIPAL_CLASS_UNRESOLVED
    return PRINCIPAL_CLASS_UNRESOLVED_FOREIGN


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
