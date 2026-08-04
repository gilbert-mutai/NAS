"""Domain enumerations. Pure values — no I/O, no framework imports."""

from __future__ import annotations

from enum import StrEnum


class Vendor(StrEnum):
    """Network device vendors.

    Phase 1 only implements a driver for ``JUNIPER``. The remaining members are
    declared so that adding a vendor is a new driver plus a registry entry, with
    no schema migration. ``MOCK`` backs local development and CI, where no real
    switch is reachable.
    """

    JUNIPER = "juniper"
    CISCO = "cisco"
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
_IMPLEMENTED_VENDORS: frozenset[Vendor] = frozenset({Vendor.JUNIPER, Vendor.MOCK})

_VENDOR_LABELS: dict[Vendor, str] = {
    Vendor.JUNIPER: "Juniper",
    Vendor.CISCO: "Cisco",
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
