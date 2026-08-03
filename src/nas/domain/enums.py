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
