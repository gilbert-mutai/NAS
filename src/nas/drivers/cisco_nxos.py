"""Cisco NX-OS driver (Nexus 9000).

Transport is **NX-API: JSON over HTTPS**, parsed by ``cisco_nxos_parser``.

This is the best-supported of the three platforms. ``show vlan | json`` returns
real structured data, so there is no screen-scraping and no per-release output
drift to chase. It also needs no thread offload: ``httpx`` is natively async, so
unlike the PyEZ and netmiko drivers this one runs directly on the event loop.

Requirements on the device:

* ``feature nxapi`` enabled (and ``nxapi https port 443``).
* An account with at least the ``network-operator`` role. VXLAN VNI enrichment
  additionally needs permission for ``show running-config vlan``; without it VLANs
  still sync, just without VNIs.
"""

from __future__ import annotations

from typing import Any

import httpx

from nas.core.credentials import AuthMethod, DeviceCredential
from nas.core.logging import get_logger
from nas.domain.entities import Switch
from nas.drivers.base import (
    DeviceFacts,
    DiscoveredVlan,
    DriverAuthenticationError,
    DriverConnectionError,
    DriverError,
    DriverParseError,
)
from nas.drivers.cisco_nxos_parser import (
    enrich,
    parse_show_version,
    parse_show_vlan,
    parse_svi_interfaces,
    parse_vn_segments,
)
from nas.drivers.options import DriverOptions

logger = get_logger(__name__)

NXAPI_PATH = "/ins"
DEFAULT_HTTPS_PORT = 443

CMD_VLAN = "show vlan"
CMD_SVI = "show interface brief"
CMD_VERSION = "show version"
CMD_RUNNING_VLAN = "show running-config vlan"


class CiscoNxosDriver:
    """Read-only NX-API session against a Nexus device."""

    def __init__(
        self,
        switch: Switch,
        credential: DeviceCredential,
        options: DriverOptions | None = None,
    ) -> None:
        self._switch = switch
        self._credential = credential
        self._options = options or DriverOptions()
        self._client: httpx.AsyncClient | None = None

    @property
    def label(self) -> str:
        return f"{self._switch.name} ({self._switch.hostname})"

    @property
    def _base_url(self) -> str:
        return f"https://{self._switch.hostname}:{self._switch.port}"

    def _check_configuration(self) -> None:
        """Fail loudly on a misregistered device rather than guessing a port.

        Port 22 means somebody registered a Nexus with the SSH default. NX-API
        listens on 443, so silently substituting it would connect somewhere the
        operator did not specify — better to say what is wrong and how to fix it.
        """
        if self._switch.port == 22:
            raise DriverError(
                f"{self.label} is registered on port 22, but the NX-OS driver uses "
                f"NX-API over HTTPS (normally port {DEFAULT_HTTPS_PORT}). Re-register it "
                f"with: nas switch add --name {self._switch.name} "
                f"--hostname {self._switch.hostname} --vendor cisco_nxos "
                f"--port {DEFAULT_HTTPS_PORT} --credential-ref {self._switch.credential_ref}"
            )
        if self._credential.auth_method is not AuthMethod.PASSWORD:
            raise DriverError(
                f"Credential {self._credential.ref!r} for {self.label} uses "
                f"{self._credential.auth_method.value} auth, but NX-API authenticates "
                "with a username and password."
            )

    # ── Connection ────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        self._check_configuration()

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(self._credential.username, self._credential.password or ""),
            verify=self._options.verify_tls,
            timeout=httpx.Timeout(
                connect=self._options.connect_timeout,
                read=self._options.command_timeout,
                write=self._options.command_timeout,
                pool=self._options.connect_timeout,
            ),
            headers={"Content-Type": "application/json"},
        )

        # Prove reachability and credentials now, so a bad switch fails at connect
        # like the other drivers rather than midway through discovery.
        await self._invoke(CMD_VERSION, output_format="json")
        logger.debug("cisco_nxos_connected", switch=self._switch.name)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise DriverConnectionError(f"Not connected to {self.label}.")
        return self._client

    # ── NX-API plumbing ───────────────────────────────────────────────────────
    async def _invoke(self, command: str, *, output_format: str = "json") -> Any:
        """Run one command through NX-API and return the decoded payload.

        ``output_format="json"`` yields structured data; ``"cli_show_ascii"`` yields
        text, needed for ``show running-config vlan``.
        """
        client = self._require_client()
        payload = {
            "ins_api": {
                "version": "1.0",
                "type": "cli_show" if output_format == "json" else "cli_show_ascii",
                "chunk": "0",
                "sid": "1",
                "input": command,
                "output_format": "json",
            }
        }

        try:
            response = await client.post(NXAPI_PATH, json=payload)
        except httpx.TimeoutException as exc:
            raise DriverConnectionError(f"{self.label} did not respond to NX-API in time.") from exc
        except httpx.HTTPError as exc:
            raise DriverConnectionError(f"NX-API request to {self.label} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise DriverAuthenticationError(
                f"NX-API credentials rejected by {self.label} "
                f"(HTTP {response.status_code}). Check the account's role."
            )
        if response.status_code == 404:
            raise DriverError(
                f"NX-API endpoint not found on {self.label}. Is 'feature nxapi' enabled?"
            )
        if response.status_code >= 400:
            raise DriverError(f"NX-API on {self.label} returned HTTP {response.status_code}.")

        try:
            return response.json()
        except ValueError as exc:
            raise DriverParseError(f"NX-API on {self.label} returned a non-JSON body.") from exc

    # ── Reads ─────────────────────────────────────────────────────────────────
    async def get_facts(self) -> DeviceFacts:
        try:
            payload = await self._invoke(CMD_VERSION)
        except DriverError as exc:
            logger.debug("nxos_version_unavailable", switch=self._switch.name, error=str(exc))
            return DeviceFacts(hostname=self._switch.name)

        model, version = parse_show_version(payload)
        return DeviceFacts(hostname=self._switch.name, model=model, os_version=version)

    async def get_vlans(self) -> tuple[DiscoveredVlan, ...]:
        # Must succeed or raise: a partial list would look like deletions.
        vlans = parse_show_vlan(await self._invoke(CMD_VLAN))

        svis: dict[int, str] = {}
        try:
            svis = parse_svi_interfaces(await self._invoke(CMD_SVI))
        except DriverError as exc:
            logger.debug("nxos_svi_unavailable", switch=self._switch.name, error=str(exc))

        # VXLAN enrichment needs `show running-config vlan`, which a
        # network-operator role may not permit. Absent VNIs are not a failure.
        vnis: dict[int, int] = {}
        try:
            raw = await self._invoke(CMD_RUNNING_VLAN, output_format="ascii")
            vnis = parse_vn_segments(_ascii_body(raw))
        except DriverError as exc:
            logger.debug("nxos_vn_segment_unavailable", switch=self._switch.name, error=str(exc))

        enriched = enrich(vlans, vnis=vnis, svis=svis)
        logger.info(
            "cisco_nxos_vlans_discovered",
            switch=self._switch.name,
            count=len(enriched),
            svis=len(svis),
            vnis=len(vnis),
        )
        return enriched


def _ascii_body(payload: Any) -> str:
    """Pull the text body out of a cli_show_ascii response."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    envelope = payload.get("ins_api")
    if isinstance(envelope, dict):
        outputs = envelope.get("outputs")
        if isinstance(outputs, dict):
            entry = outputs.get("output")
            if isinstance(entry, list):
                entry = entry[0] if entry else None
            if isinstance(entry, dict):
                body = entry.get("body")
                return body if isinstance(body, str) else ""
    body = payload.get("body")
    return body if isinstance(body, str) else ""
