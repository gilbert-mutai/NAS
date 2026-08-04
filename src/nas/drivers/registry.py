"""Driver registry — the single place a vendor is wired in.

Adding Cisco, Arista, MikroTik, HP or Huawei means writing one module and adding
one line to ``_FACTORIES``. Nothing in the services, the reconciliation engine or
the API changes. That is the whole point of the abstraction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from nas.core.credentials import DeviceCredential
from nas.core.logging import get_logger
from nas.domain.entities import Switch
from nas.domain.enums import Vendor
from nas.drivers.base import DriverNotSupportedError, NetworkDeviceDriver
from nas.drivers.juniper import JuniperDriver
from nas.drivers.mock import MockDriver

logger = get_logger(__name__)

DriverFactory = Callable[[Switch, DeviceCredential, int, int], NetworkDeviceDriver]


def _juniper(
    switch: Switch, credential: DeviceCredential, connect_timeout: int, command_timeout: int
) -> NetworkDeviceDriver:
    return JuniperDriver(
        switch,
        credential,
        connect_timeout=connect_timeout,
        command_timeout=command_timeout,
    )


def _mock(
    switch: Switch, credential: DeviceCredential, connect_timeout: int, command_timeout: int
) -> NetworkDeviceDriver:
    del connect_timeout, command_timeout  # the mock opens no socket
    return MockDriver(switch, credential)


_FACTORIES: dict[Vendor, DriverFactory] = {
    Vendor.JUNIPER: _juniper,
    Vendor.MOCK: _mock,
}


def supported_vendors() -> frozenset[Vendor]:
    return frozenset(_FACTORIES)


def create_driver(
    switch: Switch,
    credential: DeviceCredential,
    *,
    connect_timeout: int = 30,
    command_timeout: int = 60,
) -> NetworkDeviceDriver:
    """Build a driver for a switch, or raise DriverNotSupportedError."""
    factory = _FACTORIES.get(switch.vendor)
    if factory is None:
        raise DriverNotSupportedError(
            f"No driver implemented for vendor {switch.vendor.value!r} "
            f"(switch {switch.name!r}). Supported: "
            f"{', '.join(sorted(v.value for v in _FACTORIES))}."
        )
    return factory(switch, credential, connect_timeout, command_timeout)


@asynccontextmanager
async def open_driver(
    switch: Switch,
    credential: DeviceCredential,
    *,
    connect_timeout: int = 30,
    command_timeout: int = 60,
) -> AsyncIterator[NetworkDeviceDriver]:
    """Connect, yield, and always close — even if the caller raises.

    Every consumer should use this rather than calling connect/close by hand; a
    leaked NETCONF session holds a slot on the switch.
    """
    driver = create_driver(
        switch,
        credential,
        connect_timeout=connect_timeout,
        command_timeout=command_timeout,
    )
    await driver.connect()
    try:
        yield driver
    finally:
        await driver.close()
