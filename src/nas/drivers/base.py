"""Device driver contract.

The vendor abstraction. A driver's only job is to talk to one device and return
**normalised** data; it knows nothing about the database, the reconciliation
engine or HTTP. Adding a vendor means writing one module and registering it —
nothing in the core changes.

Everything here is pure types plus a Protocol, so this module imports no vendor
library. That matters: `junos-eznc` is a heavy optional dependency, and the
reconciliation engine must be testable without it installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from nas.domain.entities import MAX_VLAN_ID, MIN_VLAN_ID
from nas.domain.enums import InterfaceMode


# ── Errors ────────────────────────────────────────────────────────────────────
class DriverError(Exception):
    """Base class for every device-interaction failure.

    The reconciliation engine treats *any* DriverError as "this switch's state is
    unknown this round" and refuses to mark its VLANs missing. That is why the
    hierarchy is deliberately shallow: no caller needs to distinguish these in
    order to stay safe.
    """


class DriverConnectionError(DriverError):
    """Could not reach the device (network, DNS, refused, timeout)."""


class DriverAuthenticationError(DriverError):
    """Reached the device but credentials were rejected."""


class DriverParseError(DriverError):
    """Device responded but the payload could not be understood."""


class DriverNotSupportedError(DriverError):
    """No driver is implemented for this vendor."""


class DriverDependencyError(DriverError):
    """The driver's library is not installed."""


# ── Data returned by drivers ──────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DeviceFacts:
    """Identifying information about a device."""

    hostname: str | None = None
    model: str | None = None
    os_version: str | None = None
    serial_number: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredInterface:
    """An interface carrying a VLAN, as reported by the device."""

    name: str
    mode: InterfaceMode = InterfaceMode.UNKNOWN

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DriverParseError("Interface name cannot be blank.")


@dataclass(frozen=True, slots=True)
class DiscoveredVlan:
    """One VLAN as reported by a device, normalised across vendors.

    Validation happens here, at the boundary. A device returning a nonsense VLAN
    id should fail loudly at parse time rather than reaching the database and
    tripping a check constraint mid-transaction.
    """

    vlan_id: int
    name: str | None = None
    description: str | None = None
    l3_interface: str | None = None
    vxlan_vni: int | None = None
    interfaces: tuple[DiscoveredInterface, ...] = ()
    # Whatever the driver received, kept for audit and for debugging parser
    # changes against real device output. Never interpreted by the core.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not MIN_VLAN_ID <= self.vlan_id <= MAX_VLAN_ID:
            raise DriverParseError(
                f"VLAN id {self.vlan_id} is outside the assignable range "
                f"{MIN_VLAN_ID}-{MAX_VLAN_ID}."
            )
        seen: set[str] = set()
        for interface in self.interfaces:
            key = interface.name.lower()
            if key in seen:
                raise DriverParseError(
                    f"VLAN {self.vlan_id} lists interface {interface.name!r} twice."
                )
            seen.add(key)


# ── The contract ──────────────────────────────────────────────────────────────
@runtime_checkable
class NetworkDeviceDriver(Protocol):
    """Read-only access to one network device.

    Phase 1 is discovery only. There is deliberately **no** write method on this
    interface: VLAN creation and configuration are later phases, and until then
    the type system itself guarantees this service cannot modify a switch.
    """

    @property
    def label(self) -> str:
        """Human-readable device identifier, for logs and errors."""
        ...

    async def connect(self) -> None:
        """Open a session. Raises DriverConnectionError/DriverAuthenticationError."""
        ...

    async def close(self) -> None:
        """Release the session. Must be safe to call when not connected."""
        ...

    async def get_facts(self) -> DeviceFacts:
        """Return device identity. Must be called after connect()."""
        ...

    async def get_vlans(self) -> tuple[DiscoveredVlan, ...]:
        """Return every VLAN configured on the device.

        Must return the **complete** set or raise. A partial list would look to
        the reconciliation engine like VLANs having been deleted.
        """
        ...
