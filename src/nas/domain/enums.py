"""Domain enumerations. Pure values — no I/O, no framework imports."""

from __future__ import annotations

from enum import StrEnum


class Vendor(StrEnum):
    """Network device platforms.

    These are *platforms*, not companies. Cisco Catalyst and Cisco Nexus are
    separate members because they need separate drivers: different command syntax,
    different structured-output mechanisms, even different transports (SSH CLI
    versus NX-API over HTTPS). Folding them into one "cisco" value would push a
    branch-on-platform conditional into the driver.

    Because ``switches.vendor`` is a plain string column with no CHECK constraint,
    adding a member here needs no migration — only a driver and a registry entry.
    """

    CISCO_IOSXE = "cisco_iosxe"
    """Catalyst running IOS or IOS-XE (3650, 9300, 2960, ...)."""

    CISCO_NXOS = "cisco_nxos"
    """Nexus running NX-OS (N9K, ...)."""

    JUNIPER = "juniper"
    CISCO = "cisco"
    """Legacy, ambiguous. Kept so existing rows still load; use a specific
    platform instead. Deliberately not implemented — sync reports it as skipped
    with a message naming the two replacements."""

    MIKROTIK = "mikrotik"
    ARISTA = "arista"
    HP = "hp"
    HUAWEI = "huawei"
    MOCK = "mock"

    @property
    def is_implemented(self) -> bool:
        return self in _IMPLEMENTED_VENDORS

    @property
    def label(self) -> str:
        return _VENDOR_LABELS[self]


# Kept in the domain rather than the driver package so the API can advertise
# capability without importing device-facing code.
_IMPLEMENTED_VENDORS: frozenset[Vendor] = frozenset(
    {Vendor.CISCO_IOSXE, Vendor.CISCO_NXOS, Vendor.JUNIPER, Vendor.MOCK}
)

_VENDOR_LABELS: dict[Vendor, str] = {
    Vendor.CISCO_IOSXE: "Cisco Catalyst (IOS-XE)",
    Vendor.CISCO_NXOS: "Cisco Nexus (NX-OS)",
    Vendor.JUNIPER: "Juniper",
    Vendor.CISCO: "Cisco (unspecified platform)",
    Vendor.MIKROTIK: "MikroTik",
    Vendor.ARISTA: "Arista",
    Vendor.HP: "HP",
    Vendor.HUAWEI: "Huawei",
    Vendor.MOCK: "Mock (development)",
}


class CredentialStatus(StrEnum):
    """Whether a switch's ``credential_ref`` currently resolves.

    Surfaced by the API so an operator can see that a switch is misconfigured
    without the API ever returning the secret itself.
    """

    RESOLVED = "resolved"
    MISSING = "missing"
    NOT_CONFIGURED = "not_configured"


class ReachabilityState(StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class VlanState(StrEnum):
    """Lifecycle of a discovered VLAN record.

    Removals are soft. A VLAN that disappears from a switch becomes ``MISSING``
    with its history intact, rather than being deleted — discovery data is an
    audit trail, and a VLAN can also reappear.
    """

    ACTIVE = "active"
    MISSING = "missing"


class InterfaceMode(StrEnum):
    """How an interface carries a VLAN."""

    ACCESS = "access"
    TRUNK = "trunk"
    UNKNOWN = "unknown"


class VlanAvailability(StrEnum):
    """Verdict for a VLAN id lookup.

    Availability is always **derived** from current records, never stored — a
    persisted flag would drift out of step with the switches, which is precisely
    what this service exists to prevent.
    """

    AVAILABLE = "available"
    """No active record on any switch in scope."""

    IN_USE = "in_use"
    """Active on at least one switch."""

    RESERVED = "reserved"
    """Outside the usable range (0 and 4095 are reserved by 802.1Q)."""


class SyncTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    CLI = "cli"


class SyncStatus(StrEnum):
    """Outcome of a synchronisation run.

    ``PARTIAL`` exists so a run where some switches succeeded and others failed
    is never reported as a flat success or a flat failure — the distinction is
    what tells an operator whether the VLAN data is trustworthy.
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self is not SyncStatus.RUNNING


class SwitchSyncOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Inactive, unsupported vendor, or unresolvable credential."""


class AuditAction(StrEnum):
    """What an audit entry records.

    Dotted ``subject.verb`` names so a partial match selects a family:
    ``action LIKE 'switch.%'`` finds every inventory change.

    Read endpoints are deliberately absent. The structured access log already
    records every request, and adding a row per VLAN lookup would bury the events
    that actually matter — a sync against production hardware, an inventory
    change, a rejected call — under routine traffic.
    """

    SYNC_TRIGGER = "sync.trigger"
    """A synchronisation was requested. The only Phase 1 action that reaches a
    switch, which is why it is the one that most needs attributing."""

    SWITCH_CREATE = "switch.create"
    SWITCH_UPDATE = "switch.update"
    SWITCH_DELETE = "switch.delete"

    APIKEY_CREATE = "apikey.create"
    APIKEY_REVOKE = "apikey.revoke"

    AUTH_DENIED = "auth.denied"
    """A call rejected for insufficient scope. Recorded because a key reaching for
    a privilege it was not granted is worth seeing, whether it is a
    misconfiguration or a probe."""


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    """Authenticated but not permitted."""

    ERROR = "error"
    """Permitted and attempted, but it failed."""
