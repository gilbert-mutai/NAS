"""Driver registry — the single place a platform is wired in.

Adding a platform means writing one module and adding one line to ``_FACTORIES``.
Nothing in the services, the reconciliation engine or the API changes. That claim
was tested for real when the infrastructure team revealed Cisco, not Juniper, is
the primary fleet: the two Cisco drivers landed here plus two enum members, and no
schema migration was needed because ``switches.vendor`` carries no CHECK
constraint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from nas.core.credentials import DeviceCredential
from nas.core.logging import get_logger
from nas.domain.entities import Switch
from nas.domain.enums import Vendor
from nas.drivers.base import DriverNotSupportedError, NetworkDeviceDriver
from nas.drivers.cisco_iosxe import CiscoIosXeDriver
from nas.drivers.cisco_nxos import CiscoNxosDriver
from nas.drivers.juniper import JuniperDriver
from nas.drivers.mock import MockDriver
from nas.drivers.options import DriverOptions

logger = get_logger(__name__)

DriverFactory = Callable[[Switch, DeviceCredential, DriverOptions], NetworkDeviceDriver]


def _cisco_iosxe(
    switch: Switch, credential: DeviceCredential, options: DriverOptions
) -> NetworkDeviceDriver:
    return CiscoIosXeDriver(switch, credential, options)


def _cisco_nxos(
    switch: Switch, credential: DeviceCredential, options: DriverOptions
) -> NetworkDeviceDriver:
    return CiscoNxosDriver(switch, credential, options)


def _juniper(
    switch: Switch, credential: DeviceCredential, options: DriverOptions
) -> NetworkDeviceDriver:
    return JuniperDriver(switch, credential, options)


def _mock(
    switch: Switch, credential: DeviceCredential, options: DriverOptions
) -> NetworkDeviceDriver:
    del options  # the mock opens no socket
    return MockDriver(switch, credential)


_FACTORIES: dict[Vendor, DriverFactory] = {
    Vendor.CISCO_IOSXE: _cisco_iosxe,
    Vendor.CISCO_NXOS: _cisco_nxos,
    Vendor.JUNIPER: _juniper,
    Vendor.MOCK: _mock,
}

# Guidance for platform values that cannot be driven, keyed by vendor. Without
# this, an operator who registered a switch as `cisco` would get a bare "not
# supported" and no idea what to do about it.
_UNSUPPORTED_HINTS: dict[Vendor, str] = {
    Vendor.CISCO: (
        "'cisco' is ambiguous — Catalyst and Nexus need different drivers. "
        "Re-register the switch as 'cisco_iosxe' (Catalyst/IOS-XE) or "
        "'cisco_nxos' (Nexus/NX-OS, port 443)."
    ),
}


def supported_vendors() -> frozenset[Vendor]:
    return frozenset(_FACTORIES)


def unsupported_reason(vendor: Vendor) -> str | None:
    """Explain why a platform cannot be driven, or None if it can.

    Exposed separately from ``create_driver`` because the sync service decides to
    *skip* an unsupported switch without ever constructing a driver — so without
    this the platform-specific guidance would be unreachable in the normal path,
    and an operator would only see a generic "not implemented".
    """
    if vendor in _FACTORIES:
        return None
    hint = _UNSUPPORTED_HINTS.get(vendor)
    if hint:
        return hint
    return (
        f"No driver implemented for platform {vendor.value!r}. Supported: "
        f"{', '.join(sorted(v.value for v in _FACTORIES))}."
    )


def create_driver(
    switch: Switch,
    credential: DeviceCredential,
    options: DriverOptions | None = None,
) -> NetworkDeviceDriver:
    """Build a driver for a switch, or raise DriverNotSupportedError."""
    factory = _FACTORIES.get(switch.vendor)
    if factory is None:
        hint = _UNSUPPORTED_HINTS.get(switch.vendor)
        detail = (
            hint
            if hint
            else (f"Supported platforms: {', '.join(sorted(v.value for v in _FACTORIES))}.")
        )
        raise DriverNotSupportedError(
            f"No driver implemented for platform {switch.vendor.value!r} "
            f"(switch {switch.name!r}). {detail}"
        )
    return factory(switch, credential, options or DriverOptions())


@asynccontextmanager
async def open_driver(
    switch: Switch,
    credential: DeviceCredential,
    options: DriverOptions | None = None,
) -> AsyncIterator[NetworkDeviceDriver]:
    """Connect, yield, and always close — even if the caller raises.

    Every consumer should use this rather than calling connect/close by hand; a
    leaked session holds a slot on the device.
    """
    driver = create_driver(switch, credential, options)
    await driver.connect()
    try:
        yield driver
    finally:
        await driver.close()
