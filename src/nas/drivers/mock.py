"""Mock driver for local development and CI.

No real switch is reachable from a developer laptop or from a CI runner, so
without this the reconciliation engine, the sync service and the whole API
surface would be untestable. It is a first-class part of the design, not a stub.

Behaviour is **deterministic**: VLANs are derived from a SHA-256 of the switch
name, so the same switch yields the same VLANs on every run and across processes.
(Python's built-in ``hash()`` is salted per process and would not do.)

Failure injection, for exercising partial runs and the "never mark missing"
invariant locally:

* hostname contains ``unreachable`` -> DriverConnectionError
* hostname contains ``badauth``     -> DriverAuthenticationError
* hostname contains ``garbled``     -> DriverParseError

Drift injection, for exercising created/updated/marked-missing without editing
code: set ``NAS_MOCK_DRIFT`` to an integer. Changing it changes the generated
VLAN set for every switch, so a second sync produces real additions, changes and
removals.
"""

from __future__ import annotations

import hashlib
import os

from nas.core.credentials import DeviceCredential
from nas.core.logging import get_logger
from nas.domain.entities import Switch
from nas.domain.enums import InterfaceMode
from nas.drivers.base import (
    DeviceFacts,
    DiscoveredInterface,
    DiscoveredVlan,
    DriverAuthenticationError,
    DriverConnectionError,
    DriverParseError,
)

logger = get_logger(__name__)

DRIFT_ENV_VAR = "NAS_MOCK_DRIFT"

# Shaped after the real use case: SIP trunk mapping between Angani and its
# providers, plus infrastructure VLANs.
_VLAN_CATALOGUE: tuple[tuple[int, str, str], ...] = (
    (10, "mgmt", "Out-of-band management"),
    (20, "storage", "Storage replication"),
    (30, "vmotion", "Compute node migration"),
    (100, "sip-angani", "SIP trunk — Angani core"),
    (110, "sip-safaricom", "SIP trunk — Safaricom"),
    (120, "sip-airtel", "SIP trunk — Airtel"),
    (130, "sip-jtl", "SIP trunk — JTL"),
    (200, "cust-transit", "Customer transit"),
    (1001, "cust-acme-voice", "ACME Ltd — 3CX voice"),
    (1002, "cust-beta-voice", "Beta Holdings — 3CX voice"),
    (1003, "cust-gamma-voice", "Gamma Systems — 3CX voice"),
    (1010, "cust-delta-data", "Delta Co — data"),
    (2001, "cust-epsilon-voice", "Epsilon Group — 3CX voice"),
    (2002, "cust-zeta-voice", "Zeta Traders — 3CX voice"),
    (3000, "vxlan-underlay", "VXLAN underlay"),
)

_FAILURE_MARKERS: tuple[tuple[str, type[Exception], str], ...] = (
    ("unreachable", DriverConnectionError, "Simulated: host unreachable"),
    ("badauth", DriverAuthenticationError, "Simulated: authentication rejected"),
    ("garbled", DriverParseError, "Simulated: unparseable device output"),
)


def _seed(value: str) -> int:
    """Process-stable integer seed derived from a string."""
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)


def _drift() -> int:
    raw = os.environ.get(DRIFT_ENV_VAR, "0")
    try:
        return int(raw)
    except ValueError:
        logger.warning("mock_drift_invalid", value=raw)
        return 0


class MockDriver:
    """Deterministic in-memory device. Never opens a socket."""

    def __init__(self, switch: Switch, credential: DeviceCredential | None = None) -> None:
        self._switch = switch
        self._credential = credential
        self._connected = False

    @property
    def label(self) -> str:
        return f"{self._switch.name} ({self._switch.hostname}, mock)"

    async def connect(self) -> None:
        hostname = self._switch.hostname.lower()
        for marker, error_type, message in _FAILURE_MARKERS:
            if marker in hostname:
                logger.info("mock_failure_injected", switch=self._switch.name, marker=marker)
                raise error_type(f"{message} for {self.label}")
        self._connected = True
        logger.debug("mock_connected", switch=self._switch.name)

    async def close(self) -> None:
        self._connected = False

    def _require_connection(self) -> None:
        if not self._connected:
            raise DriverConnectionError(f"Not connected to {self.label}.")

    async def get_facts(self) -> DeviceFacts:
        self._require_connection()
        seed = _seed(self._switch.name)
        return DeviceFacts(
            hostname=self._switch.name,
            model=f"MOCK-EX{2200 + seed % 400}",
            os_version=f"mock-{12 + seed % 8}.{seed % 4}R1",
            serial_number=f"MOCK{seed % 1_000_000:06d}",
        )

    async def get_vlans(self) -> tuple[DiscoveredVlan, ...]:
        self._require_connection()
        seed = _seed(self._switch.name) + _drift()

        vlans: list[DiscoveredVlan] = []
        for vlan_id, name, description in _VLAN_CATALOGUE:
            # Selection is hashed per (switch, vlan) pair so each switch gets an
            # independent ~2/3 subset. A shared bitmask over one seed would make
            # one switch's VLANs a subset of another's, and the cross-switch
            # aggregation that /vlans/lookup depends on would never be exercised.
            if _seed(f"{self._switch.name}:{vlan_id}:{_drift()}") % 3 == 0:
                continue

            interfaces: list[DiscoveredInterface] = []
            port_count = 1 + (seed + vlan_id) % 3
            for port in range(port_count):
                mode = InterfaceMode.TRUNK if vlan_id >= 1000 else InterfaceMode.ACCESS
                interfaces.append(
                    DiscoveredInterface(name=f"ge-0/0/{(vlan_id + port) % 48}", mode=mode)
                )

            vlans.append(
                DiscoveredVlan(
                    vlan_id=vlan_id,
                    name=name,
                    # Drift changes descriptions so a re-sync produces updates,
                    # not just creations and removals.
                    description=(
                        description if _drift() == 0 else f"{description} (rev {_drift()})"
                    ),
                    l3_interface=f"irb.{vlan_id}" if vlan_id < 1000 else None,
                    vxlan_vni=10_000 + vlan_id if name == "vxlan-underlay" else None,
                    interfaces=tuple(interfaces),
                    raw={"source": "mock", "switch": self._switch.name, "vlan_id": vlan_id},
                )
            )

        logger.debug("mock_vlans_generated", switch=self._switch.name, count=len(vlans))
        return tuple(vlans)
