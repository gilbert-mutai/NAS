"""Parsing of Junos ``get-vlan-information`` RPC replies.

Deliberately separated from the driver and kept **pure**: it takes XML text and
returns normalised DTOs, with no PyEZ import and no I/O. That makes the riskiest
part of the Juniper integration — reading real device output — testable against
recorded fixtures, on a laptop and in CI, with no switch and no optional
dependency installed.

Two schemas are handled, because Junos changed shape:

* **ELS** (EX4300, QFX, newer) — ``l2ng-l2ald-vlan-instance-group`` elements
* **Legacy** (EX2200/EX3300, older) — ``vlan`` elements

Element names are matched by *local* name so an XML namespace on the reply makes
no difference.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring as _safe_fromstring

from nas.core.logging import get_logger
from nas.domain.entities import MAX_VLAN_ID, MIN_VLAN_ID
from nas.domain.enums import InterfaceMode
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan, DriverParseError

logger = get_logger(__name__)

# Containers that represent one VLAN, newest schema first.
_VLAN_CONTAINERS: tuple[str, ...] = ("l2ng-l2ald-vlan-instance-group", "vlan")

# Candidate child names per field, in priority order. Junos spells the same
# concept differently across releases.
_NAME_FIELDS = ("l2ng-l2rtb-vlan-name", "vlan-name")
_TAG_FIELDS = ("l2ng-l2rtb-vlan-tag", "vlan-tag", "l2ng-l2rtb-vlan-vlan-id")
_DESCRIPTION_FIELDS = ("l2ng-l2rtb-vlan-description", "vlan-description")
_L3_FIELDS = ("l2ng-l2rtb-vlan-l3-interface", "vlan-l3-interface")
_VNI_FIELDS = ("l2ng-l2rtb-vlan-vxlan-vni", "vxlan-vni", "vlan-vxlan-vni")

_MEMBER_CONTAINERS = ("l2ng-l2rtb-vlan-member", "vlan-interface", "vlan-member")
_MEMBER_NAME_FIELDS = (
    "l2ng-l2rtb-vlan-member-interface",
    "vlan-interface-name",
    "vlan-member-interface",
    "interface-name",
)
_MEMBER_MODE_FIELDS = (
    "l2ng-l2rtb-vlan-member-tagness",
    "l2ng-l2rtb-vlan-member-interface-mode",
    "vlan-interface-mode",
)


def _local_name(tag: object) -> str:
    """Strip any ``{namespace}`` prefix from an element tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _text(element: Element, candidates: tuple[str, ...]) -> str | None:
    """First non-empty text among direct or nested children matching a name."""
    for candidate in candidates:
        for child in element.iter():
            if _local_name(child.tag) != candidate:
                continue
            value = (child.text or "").strip()
            if value:
                return value
    return None


def _int(element: Element, candidates: tuple[str, ...]) -> int | None:
    raw = _text(element, candidates)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _mode(raw: str | None) -> InterfaceMode:
    """Map Junos tagness/mode wording onto the normalised enum."""
    if not raw:
        return InterfaceMode.UNKNOWN
    normalised = raw.strip().lower()
    if normalised in {"tagged", "trunk", "tagged-access"}:
        return InterfaceMode.TRUNK
    if normalised in {"untagged", "access"}:
        return InterfaceMode.ACCESS
    return InterfaceMode.UNKNOWN


def _parse_members(group: Element) -> tuple[DiscoveredInterface, ...]:
    interfaces: list[DiscoveredInterface] = []
    seen: set[str] = set()

    for child in group.iter():
        if _local_name(child.tag) not in _MEMBER_CONTAINERS:
            continue
        name = _text(child, _MEMBER_NAME_FIELDS)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            # Junos can list the same member twice (e.g. once per routing
            # instance). Deduplicate rather than letting the DTO reject it.
            continue
        seen.add(key)
        interfaces.append(
            DiscoveredInterface(name=name, mode=_mode(_text(child, _MEMBER_MODE_FIELDS)))
        )

    return tuple(interfaces)


def _parse_group(group: Element) -> DiscoveredVlan | None:
    """Convert one VLAN element, or return None if it cannot be keyed."""
    vlan_id = _int(group, _TAG_FIELDS)
    if vlan_id is None:
        # A VLAN with no tag cannot be identified or reconciled. Skipping is
        # correct; failing the whole sync over one odd entry is not.
        logger.warning("juniper_vlan_without_tag", vlan_name=_text(group, _NAME_FIELDS))
        return None

    if not MIN_VLAN_ID <= vlan_id <= MAX_VLAN_ID:
        logger.warning("juniper_vlan_id_out_of_range", vlan_id=vlan_id)
        return None

    raw: dict[str, Any] = {
        "schema": _local_name(group.tag),
        "vlan_id": vlan_id,
    }

    return DiscoveredVlan(
        vlan_id=vlan_id,
        name=_text(group, _NAME_FIELDS),
        description=_text(group, _DESCRIPTION_FIELDS),
        l3_interface=_text(group, _L3_FIELDS),
        vxlan_vni=_int(group, _VNI_FIELDS),
        interfaces=_parse_members(group),
        raw=raw,
    )


def parse_vlan_information(xml: str) -> tuple[DiscoveredVlan, ...]:
    """Parse a ``get-vlan-information`` reply into normalised VLANs.

    Raises DriverParseError if the document is not well-formed. A well-formed
    document containing no VLANs returns an empty tuple — a switch legitimately
    may have none, and that is different from a failure to read it.
    """
    if not xml or not xml.strip():
        raise DriverParseError("Empty reply from get-vlan-information.")

    try:
        # defusedxml: device output is untrusted input, and stdlib ElementTree is
        # vulnerable to entity-expansion attacks.
        root = _safe_fromstring(xml)
    except Exception as exc:
        raise DriverParseError("get-vlan-information reply is not well-formed XML.") from exc

    groups = [element for element in root.iter() if _local_name(element.tag) in _VLAN_CONTAINERS]

    vlans: list[DiscoveredVlan] = []
    seen_ids: set[int] = set()
    for group in groups:
        parsed = _parse_group(group)
        if parsed is None:
            continue
        if parsed.vlan_id in seen_ids:
            # The same VLAN can appear once per routing instance. Identity is
            # (switch, vlan_id), so the first occurrence wins.
            logger.debug("juniper_duplicate_vlan_id", vlan_id=parsed.vlan_id)
            continue
        seen_ids.add(parsed.vlan_id)
        vlans.append(parsed)

    logger.debug("juniper_vlans_parsed", count=len(vlans), groups=len(groups))
    return tuple(vlans)
