"""Juniper driver, built on PyEZ (``junos-eznc``).

Chosen over screen-scraping the CLI because PyEZ speaks NETCONF and returns
structured XML. Parsing ``show vlans`` text output would break on Junos version
changes and table-width differences — an unacceptable foundation for data the CRM
will treat as authoritative.

Two implementation notes that matter:

1. **PyEZ is synchronous.** Every call is dispatched to a worker thread with
   ``anyio.to_thread.run_sync``; blocking the event loop would stall every
   in-flight HTTP request while a switch is being polled.
2. **PyEZ is an optional dependency**, imported lazily. The core, the
   reconciliation engine and the whole test suite work without it installed;
   only this module needs it, and it raises DriverDependencyError if absent.

The XML parsing lives in ``juniper_parser`` as a pure function so it can be
tested against recorded device output.
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
    DriverParseError,
)
from nas.drivers.juniper_parser import parse_vlan_information
from nas.drivers.options import DriverOptions

logger = get_logger(__name__)


def _load_pyez() -> tuple[Any, Any]:
    """Import PyEZ lazily, returning (Device, exception_module)."""
    try:
        from jnpr.junos import Device
        from jnpr.junos import exception as pyez_exceptions
    except ImportError as exc:
        raise DriverDependencyError(
            "junos-eznc is not installed. Install the optional extra: pip install '.[juniper]'"
        ) from exc
    return Device, pyez_exceptions


class JuniperDriver:
    """Read-only NETCONF session against a Junos device."""

    def __init__(
        self,
        switch: Switch,
        credential: DeviceCredential,
        options: DriverOptions | None = None,
    ) -> None:
        self._switch = switch
        self._credential = credential
        self._options = options or DriverOptions()
        self._device: Any | None = None

    @property
    def label(self) -> str:
        return f"{self._switch.name} ({self._switch.hostname})"

    # ── Connection ────────────────────────────────────────────────────────────
    def _build_device(self) -> Any:
        device_cls, _ = _load_pyez()

        kwargs: dict[str, Any] = {
            "host": self._switch.hostname,
            "port": self._switch.port,
            "user": self._credential.username,
            # normalize=True strips whitespace from element text, which Junos
            # pads inconsistently between releases.
            "normalize": True,
            "conn_open_timeout": self._options.connect_timeout,
            # Never fall back to an agent or ~/.ssh keys: the only credentials
            # this process may use are the ones explicitly provisioned for it.
            "ssh_config": False,
        }

        if self._credential.auth_method is AuthMethod.SSH_KEY:
            kwargs["ssh_private_key_file"] = str(self._credential.private_key_path)
            if self._credential.private_key_passphrase:
                kwargs["passwd"] = self._credential.private_key_passphrase
        else:
            kwargs["passwd"] = self._credential.password

        return device_cls(**kwargs)

    def _open_blocking(self) -> Any:
        _, pyez_exceptions = _load_pyez()
        device = self._build_device()
        try:
            device.open()
        except pyez_exceptions.ConnectAuthError as exc:
            raise DriverAuthenticationError(f"Authentication rejected by {self.label}.") from exc
        except (
            pyez_exceptions.ConnectTimeoutError,
            pyez_exceptions.ConnectRefusedError,
            pyez_exceptions.ProbeError,
            pyez_exceptions.ConnectUnknownHostError,
        ) as exc:
            raise DriverConnectionError(f"Could not reach {self.label}: {exc}") from exc
        except pyez_exceptions.ConnectError as exc:
            raise DriverConnectionError(f"Connection to {self.label} failed: {exc}") from exc
        except Exception as exc:
            raise DriverConnectionError(f"Connection to {self.label} failed: {exc}") from exc

        device.timeout = self._options.command_timeout
        return device

    async def connect(self) -> None:
        self._device = await anyio.to_thread.run_sync(self._open_blocking)
        logger.debug("juniper_connected", switch=self._switch.name)

    async def close(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return

        def _close() -> None:
            try:
                device.close()
            except Exception as exc:
                logger.warning("juniper_close_failed", switch=self._switch.name, error=str(exc))

        await anyio.to_thread.run_sync(_close)

    def _require_device(self) -> Any:
        if self._device is None:
            raise DriverConnectionError(f"Not connected to {self.label}.")
        return self._device

    # ── Reads ─────────────────────────────────────────────────────────────────
    async def get_facts(self) -> DeviceFacts:
        device = self._require_device()

        def _facts() -> DeviceFacts:
            raw = dict(device.facts or {})
            version = raw.get("version") or raw.get("junos_info") or None
            return DeviceFacts(
                hostname=_as_str(raw.get("hostname")),
                model=_as_str(raw.get("model")),
                os_version=_as_str(version),
                serial_number=_as_str(raw.get("serialnumber")),
            )

        try:
            return await anyio.to_thread.run_sync(_facts)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"Could not read facts from {self.label}: {exc}") from exc

    async def get_vlans(self) -> tuple[DiscoveredVlan, ...]:
        device = self._require_device()
        _, pyez_exceptions = _load_pyez()

        def _fetch_xml() -> str:
            from lxml import etree

            try:
                reply = device.rpc.get_vlan_information()
            except pyez_exceptions.RpcError as exc:
                raise DriverError(f"get-vlan-information failed on {self.label}: {exc}") from exc
            except pyez_exceptions.ConnectClosedError as exc:
                raise DriverConnectionError(f"Session to {self.label} closed mid-request.") from exc
            except Exception as exc:
                raise DriverError(f"get-vlan-information failed on {self.label}: {exc}") from exc

            if reply is None:
                raise DriverParseError(f"{self.label} returned no VLAN information.")
            text: str = etree.tostring(reply, encoding="unicode")
            return text

        xml = await anyio.to_thread.run_sync(_fetch_xml)
        vlans = parse_vlan_information(xml)
        logger.info("juniper_vlans_discovered", switch=self._switch.name, count=len(vlans))
        return vlans


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
