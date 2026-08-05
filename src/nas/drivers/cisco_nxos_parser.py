"""Parsing of Cisco NX-OS structured output (Nexus 9000).

Pure functions: payload in, DTOs out. No HTTP client, no I/O.

NX-OS is the *easiest* of the three platforms because NX-API returns real JSON —
``show vlan | json`` needs no screen-scraping at all. The awkwardness is entirely
in NX-OS's JSON conventions, which this module normalises:

* **Single rows collapse to an object.** ``ROW_vlanbriefxbrief`` is a list when
  there are several VLANs and a bare object when there is one. Code that assumes a
  list works in the lab and breaks on a switch with one VLAN.
* **Scalar-or-list fields.** ``vlanshowplist-ifidx`` is a string for a short port
  list and a list of strings for a long one.
* **The ins_api envelope.** Responses may arrive wrapped in
  ``ins_api.outputs.output.body`` or as a bare body, depending on how the caller
  invoked NX-API. Both are accepted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nas.core.logging import get_logger
from nas.domain.entities import MAX_VLAN_ID, MIN_VLAN_ID
from nas.domain.enums import InterfaceMode
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan, DriverParseError

logger = get_logger(__name__)

_VLAN_TABLE = "TABLE_vlanbriefxbrief"
_VLAN_ROW = "ROW_vlanbriefxbrief"

_FIELD_ID = "vlanshowbr-vlanid"
_FIELD_ID_UTF = "vlanshowbr-vlanid-utf"
_FIELD_NAME = "vlanshowbr-vlanname"
_FIELD_STATE = "vlanshowbr-vlanstate"
_FIELD_SHUT = "vlanshowbr-shutstate"
_FIELD_PORTS = "vlanshowplist-ifidx"

# `vlan 110` ... `vn-segment 10110` inside `show running-config vlan`
_VLAN_BLOCK_RE = re.compile(r"^\s*vlan\s+(\d{1,4})\s*$", re.IGNORECASE)
_VN_SEGMENT_RE = re.compile(r"^\s*vn-segment\s+(\d+)\s*$", re.IGNORECASE)


def _as_list(value: Any) -> list[Any]:
    """Normalise NX-OS's scalar-or-list convention."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _unwrap(payload: Any) -> dict[str, Any]:
    """Return the command body, accepting either a bare body or an ins_api envelope.

    Raises DriverParseError when NX-API reports a non-success code, so a device
    refusing the command is a failed read rather than an empty VLAN list — which
    the reconciler would otherwise be asked to treat as "all VLANs deleted".
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise DriverParseError("NX-API response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise DriverParseError("NX-API response is not a JSON object.")

    envelope = payload.get("ins_api")
    if not isinstance(envelope, dict):
        return payload

    outputs = envelope.get("outputs")
    if not isinstance(outputs, dict):
        raise DriverParseError("NX-API envelope has no outputs.")

    entries = _as_list(outputs.get("output"))
    if not entries:
        raise DriverParseError("NX-API envelope contains no command output.")

    first = entries[0]
    if not isinstance(first, dict):
        raise DriverParseError("NX-API command output is malformed.")

    code = str(first.get("code", "200"))
    if code != "200":
        message = first.get("msg") or first.get("clierror") or "unknown error"
        raise DriverParseError(f"NX-API rejected the command (code {code}): {message}")

    body = first.get("body")
    if body in (None, ""):
        # A successful command with an empty body: a switch with no VLANs at all.
        # Legitimate, and different from a failure — return empty, do not raise.
        return {}
    if not isinstance(body, dict):
        raise DriverParseError("NX-API command body is not a JSON object.")
    return body


def _vlan_id(row: dict[str, Any]) -> int | None:
    raw = row.get(_FIELD_ID)
    if raw in (None, ""):
        raw = row.get(_FIELD_ID_UTF)
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _ports(row: dict[str, Any]) -> tuple[DiscoveredInterface, ...]:
    """Build interfaces from the port list.

    Ranges like ``Ethernet1/1-10`` are kept intact: the device compressed them, and
    expanding would invent names that may not exist.

    Mode is UNKNOWN — this table lists a VLAN's member ports without reliably
    distinguishing access from trunk. Consistency matters more than a guess here,
    because the reconciler compares (name, mode) pairs.
    """
    entries: list[str] = []
    for chunk in _as_list(row.get(_FIELD_PORTS)):
        text = str(chunk).strip()
        if not text:
            continue
        entries.extend(part.strip() for part in text.split(",") if part.strip())

    interfaces: list[DiscoveredInterface] = []
    seen: set[str] = set()
    for name in entries:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        interfaces.append(DiscoveredInterface(name=name, mode=InterfaceMode.UNKNOWN))
    return tuple(interfaces)


def parse_show_vlan(payload: Any) -> tuple[DiscoveredVlan, ...]:
    """Parse ``show vlan | json``.

    A well-formed response describing no VLANs returns an empty tuple; only a
    malformed or rejected response raises.
    """
    body = _unwrap(payload)
    if not body:
        return ()

    table = body.get(_VLAN_TABLE)
    if table is None:
        # Some releases nest the table one level deeper.
        for value in body.values():
            if isinstance(value, dict) and _VLAN_TABLE in value:
                table = value[_VLAN_TABLE]
                break

    if table is None:
        logger.warning("nxos_vlan_table_missing", keys=sorted(body)[:10])
        return ()

    rows: list[Any] = []
    for candidate in _as_list(table):
        if isinstance(candidate, dict):
            rows.extend(_as_list(candidate.get(_VLAN_ROW)))

    vlans: list[DiscoveredVlan] = []
    seen: set[int] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        tag = _vlan_id(row)
        if tag is None:
            logger.warning("nxos_vlan_without_id")
            continue
        if not MIN_VLAN_ID <= tag <= MAX_VLAN_ID:
            logger.warning("nxos_vlan_id_out_of_range", vlan_id=tag)
            continue
        if tag in seen:
            continue
        seen.add(tag)

        name = row.get(_FIELD_NAME)
        vlans.append(
            DiscoveredVlan(
                vlan_id=tag,
                name=str(name).strip() if name not in (None, "") else None,
                description=None,  # NX-OS has no per-VLAN description here
                l3_interface=None,  # SVIs come from enrichment
                vxlan_vni=None,  # from enrich_with_vnis
                interfaces=_ports(row),
                raw={
                    "source": "show vlan | json",
                    "vlan_id": tag,
                    "state": row.get(_FIELD_STATE),
                    "shutstate": row.get(_FIELD_SHUT),
                },
            )
        )

    logger.debug("nxos_vlans_parsed", count=len(vlans))
    return tuple(vlans)


def parse_vn_segments(output: str) -> dict[int, int]:
    """Map VLAN id -> VXLAN VNI from ``show running-config vlan``.

    Best effort and never raises: VXLAN may not be configured, and the command may
    need a role the sync account lacks. A switch without this simply reports no
    VNIs, which is different from a failed read.
    """
    vnis: dict[int, int] = {}
    if not output:
        return vnis

    current: int | None = None
    for line in output.splitlines():
        vlan_match = _VLAN_BLOCK_RE.match(line)
        if vlan_match:
            current = int(vlan_match.group(1))
            continue
        if current is None:
            continue
        segment_match = _VN_SEGMENT_RE.match(line)
        if segment_match:
            vnis[current] = int(segment_match.group(1))
            current = None
        elif line.strip() and not line.startswith((" ", "\t")):
            # Left the vlan block.
            current = None

    logger.debug("nxos_vn_segments_parsed", count=len(vnis))
    return vnis


def parse_svi_interfaces(payload: Any) -> dict[int, str]:
    """Map VLAN id -> SVI name from ``show interface brief | json``.

    Best effort; never raises.
    """
    svis: dict[int, str] = {}
    try:
        body = _unwrap(payload)
    except DriverParseError:
        return svis
    if not body:
        return svis

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"interface", "vlan-interface", "svi-if-name"} and isinstance(value, str):
                    match = re.match(r"^Vlan(\d{1,4})$", value.strip(), re.IGNORECASE)
                    if match:
                        tag = int(match.group(1))
                        if MIN_VLAN_ID <= tag <= MAX_VLAN_ID:
                            svis[tag] = value.strip()
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    logger.debug("nxos_svis_parsed", count=len(svis))
    return svis


def enrich(
    vlans: tuple[DiscoveredVlan, ...],
    *,
    vnis: dict[int, int] | None = None,
    svis: dict[int, str] | None = None,
) -> tuple[DiscoveredVlan, ...]:
    """Attach VNI and SVI information where available."""
    if not vnis and not svis:
        return vlans

    vnis = vnis or {}
    svis = svis or {}
    enriched: list[DiscoveredVlan] = []

    for vlan in vlans:
        vni = vnis.get(vlan.vlan_id)
        svi = svis.get(vlan.vlan_id)
        if vni is None and svi is None:
            enriched.append(vlan)
            continue
        extra: dict[str, Any] = {}
        if vni is not None:
            extra["vn_segment"] = vni
        if svi is not None:
            extra["svi"] = svi
        enriched.append(
            DiscoveredVlan(
                vlan_id=vlan.vlan_id,
                name=vlan.name,
                description=vlan.description,
                l3_interface=svi or vlan.l3_interface,
                vxlan_vni=vni if vni is not None else vlan.vxlan_vni,
                interfaces=vlan.interfaces,
                raw={**vlan.raw, **extra},
            )
        )
    return tuple(enriched)


def parse_show_version(payload: Any) -> tuple[str | None, str | None]:
    """Extract (model, os_version) from ``show version | json``. Best effort."""
    try:
        body = _unwrap(payload)
    except DriverParseError:
        return None, None
    if not body:
        return None, None

    model = body.get("chassis_id") or body.get("modelnum") or None
    version = (
        body.get("nxos_ver_str") or body.get("kickstart_ver_str") or body.get("sys_ver_str") or None
    )
    return (
        str(model).strip() if model else None,
        str(version).strip() if version else None,
    )
