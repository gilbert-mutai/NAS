"""Cisco IOS / IOS-XE driver (Catalyst 3650, 9300, 2960, ...).

Transport is **SSH CLI via netmiko**, parsed by ``cisco_iosxe_parser``.

Why not NETCONF/YANG: IOS-XE only gained usable NETCONF in 16.x, it is disabled by
default, and Catalyst 3650s in the field run anything from 3.x upward. CLI parsing
works on all of them, so the firmware version is not a prerequisite for shipping.
NETCONF is a worthwhile optimisation later for the subset that supports it.

Why netmiko rather than raw paramiko: network CLIs need pager suppression
(``terminal length 0``), prompt detection, and per-platform quirks. netmiko does
that; hand-rolling it on paramiko means reimplementing a solved problem badly.

netmiko is synchronous, so every call is dispatched to a worker thread — same
pattern as the Juniper driver. Blocking the event loop would stall every in-flight
HTTP request while a switch is polled.
"""

from __future__ import annotations

from typing import Any

import anyio.to_thread

from nas.core.credentials import AuthMethod, DeviceCredential
from nas.core.logging import get_logger
from nas.domain.entities import Switch
from nas.drivers.base import (
    DeviceFacts,
    DiscoveredVlan,
    DriverAuthenticationError,
    DriverConnectionError,
    DriverDependencyError,
    DriverError,
)
from nas.drivers.cisco_iosxe_parser import (
    enrich_with_svis,
    parse_show_vlan_brief,
    parse_svi_interfaces,
    parse_version,
)
from nas.drivers.options import DriverOptions

logger = get_logger(__name__)

# netmiko's platform key. "cisco_ios" drives both IOS and IOS-XE.
NETMIKO_DEVICE_TYPE = "cisco_ios"

CMD_VLAN = "show vlan brief"
CMD_SVI = "show ip interface brief"
CMD_VERSION = "show version"


def _load_netmiko() -> tuple[Any, Any]:
    try:
        from netmiko import ConnectHandler
        from netmiko import exceptions as netmiko_exceptions
    except ImportError as exc:
        raise DriverDependencyError(
            "netmiko is not installed. Install the optional extra: pip install '.[cisco]'"
        ) from exc
    return ConnectHandler, netmiko_exceptions


class CiscoIosXeDriver:
    """Read-only SSH session against an IOS/IOS-XE device."""

    def __init__(
        self,
        switch: Switch,
        credential: DeviceCredential,
        options: DriverOptions | None = None,
    ) -> None:
        self._switch = switch
        self._credential = credential
        self._options = options or DriverOptions()
        self._connection: Any | None = None

    @property
    def label(self) -> str:
        return f"{self._switch.name} ({self._switch.hostname})"

    # ── Connection ────────────────────────────────────────────────────────────
    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "device_type": NETMIKO_DEVICE_TYPE,
            "host": self._switch.hostname,
            "port": self._switch.port,
            "username": self._credential.username,
            "conn_timeout": self._options.connect_timeout,
            "timeout": self._options.command_timeout,
            # Never fall back to an agent or ~/.ssh: the only credentials this
            # process may use are the ones explicitly provisioned for it.
            "use_keys": self._credential.auth_method is AuthMethod.SSH_KEY,
            "allow_agent": False,
        }

        if self._credential.auth_method is AuthMethod.SSH_KEY:
            kwargs["key_file"] = str(self._credential.private_key_path)
            if self._credential.private_key_passphrase:
                kwargs["passphrase"] = self._credential.private_key_passphrase
        else:
            kwargs["password"] = self._credential.password

        # Only supplied when configured. Discovery runs happily at privilege 1 on
        # most estates; an enable secret is only needed where `show` commands are
        # restricted.
        if self._credential.enable_password:
            kwargs["secret"] = self._credential.enable_password

        return kwargs

    def _open_blocking(self) -> Any:
        connect_handler, netmiko_exceptions = _load_netmiko()
        try:
            connection = connect_handler(**self._connect_kwargs())
        except netmiko_exceptions.NetmikoAuthenticationException as exc:
            raise DriverAuthenticationError(f"Authentication rejected by {self.label}.") from exc
        except netmiko_exceptions.NetmikoTimeoutException as exc:
            raise DriverConnectionError(f"Could not reach {self.label}: {exc}") from exc
        except Exception as exc:
            raise DriverConnectionError(f"Connection to {self.label} failed: {exc}") from exc

        if self._credential.enable_password:
            try:
                connection.enable()
            except Exception as exc:
                # Discovery usually does not need privileged mode, so log and carry
                # on rather than failing the switch outright.
                logger.warning("cisco_enable_failed", switch=self._switch.name, error=str(exc))

        return connection

    async def connect(self) -> None:
        self._connection = await anyio.to_thread.run_sync(self._open_blocking)
        logger.debug("cisco_iosxe_connected", switch=self._switch.name)

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return

        def _close() -> None:
            try:
                connection.disconnect()
            except Exception as exc:
                logger.warning("cisco_close_failed", switch=self._switch.name, error=str(exc))

        await anyio.to_thread.run_sync(_close)

    def _require_connection(self) -> Any:
        if self._connection is None:
            raise DriverConnectionError(f"Not connected to {self.label}.")
        return self._connection

    def _send_blocking(self, command: str) -> str:
        connection = self._require_connection()
        _, netmiko_exceptions = _load_netmiko()
        try:
            output = connection.send_command(command, read_timeout=self._options.command_timeout)
        except netmiko_exceptions.NetmikoTimeoutException as exc:
            raise DriverConnectionError(
                f"{self.label} stopped responding during '{command}'."
            ) from exc
        except Exception as exc:
            raise DriverError(f"'{command}' failed on {self.label}: {exc}") from exc
        return str(output)

    async def _send(self, command: str) -> str:
        return await anyio.to_thread.run_sync(self._send_blocking, command)

    # ── Reads ─────────────────────────────────────────────────────────────────
    async def get_facts(self) -> DeviceFacts:
        try:
            output = await self._send(CMD_VERSION)
        except DriverError as exc:
            # Facts are cosmetic; never fail a sync over a banner.
            logger.debug("cisco_version_unavailable", switch=self._switch.name, error=str(exc))
            return DeviceFacts(hostname=self._switch.name)

        model, version = parse_version(output)
        return DeviceFacts(hostname=self._switch.name, model=model, os_version=version)

    async def get_vlans(self) -> tuple[DiscoveredVlan, ...]:
        # This one must succeed or raise — a partial list would look to the
        # reconciliation engine like VLANs having been deleted.
        vlans = parse_show_vlan_brief(await self._send(CMD_VLAN))

        # SVI lookup is enrichment. If the device or the account will not do it,
        # report VLANs without L3 detail rather than failing the switch.
        try:
            svis = parse_svi_interfaces(await self._send(CMD_SVI))
        except DriverError as exc:
            logger.debug("cisco_svi_unavailable", switch=self._switch.name, error=str(exc))
            svis = {}

        enriched = enrich_with_svis(vlans, svis)
        logger.info(
            "cisco_iosxe_vlans_discovered",
            switch=self._switch.name,
            count=len(enriched),
            svis=len(svis),
        )
        return enriched
