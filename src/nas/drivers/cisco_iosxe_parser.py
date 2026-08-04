"""Parsing of Cisco IOS / IOS-XE ``show`` output.

Pure functions: text in, DTOs out. No SSH library, no I/O. Same reasoning as the
Junos parser — this is the riskiest part of the integration, and keeping it pure
makes it testable against recorded output from a real 3650 with no device access
and no optional dependency installed.

**Why CLI text and not NETCONF.** IOS-XE only gained usable NETCONF/YANG in 16.x,
and it is off by default. Catalyst 3650s in the field run anything from 3.x to
16.x. Parsing ``show vlan brief`` works on every one of them, so the platform's
firmware version stops being a prerequisite. NETCONF is a later optimisation for
the subset that supports it, not a gate on shipping.

**Why a hand-written parser and not TextFSM/ntc-templates.** ntc-templates is the
industry standard and would be the right call if we needed dozens of commands
across many platforms. For two stable, well-known commands it would add a large
dependency plus a template-resolution layer for ~150 lines of parsing. The column
geometry is derived from the separator row rather than hardcoded, which is what
makes this robust across platforms with different field widths.
"""

from __future__ import annotations

import re
from typing import Any

from nas.core.logging import get_logger
from nas.domain.entities import MAX_VLAN_ID, MIN_VLAN_ID
from nas.domain.enums import InterfaceMode
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan, DriverParseError

logger = get_logger(__name__)

# Matches the dashed separator row under the header, e.g.
#   ---- -------------------------------- --------- ------------------------------
_SEPARATOR_RE = re.compile(r"^-{2,}(\s+-{2,})+\s*$")

# "Vlan110", "Vlan1" — an SVI, which is IOS's equivalent of a Junos irb unit.
_SVI_RE = re.compile(r"^Vlan(\d{1,4})$", re.IGNORECASE)

# VLANs IOS creates itself and that carry no customer meaning. Reported by the
# device but noise in an inventory, and 1002-1005 do not even exist on most
# modern switches.
_DEFAULT_VLAN_NAMES = frozenset(
    {"fddi-default", "token-ring-default", "fddinet-default", "trnet-default", "trcrf-default"}
)


def _column_bounds(separator: str) -> list[tuple[int, int]]:
    """Derive column slices from the dashed separator row.

    Using the device's own column geometry rather than fixed offsets is what makes
    this survive platforms that pad ``Name`` to a different width.
    """
    bounds: list[tuple[int, int]] = []
    for match in re.finditer(r"-+", separator):
        bounds.append((match.start(), match.end()))
    return bounds


def _slice(line: str, start: int, end: int | None) -> str:
    return line[start:end].strip() if start < len(line) else ""


def _split_ports(raw: str) -> list[str]:
    """Split a Ports cell into interface names.

    Commas separate entries. Ranges such as ``Gi1/0/1-4`` are left intact: the
    device compressed them, and expanding would invent interface names that may
    not exist (Catalyst numbering is not always contiguous).
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_show_vlan_brief(output: str) -> tuple[DiscoveredVlan, ...]:
    """Parse ``show vlan brief``.

    Handles the two things that make this output awkward:

    * **Continuation rows.** A VLAN with many ports wraps onto following lines with
      the VLAN/Name/Status columns blank; those ports belong to the VLAN above.
    * **Variable column widths**, derived from the separator row.

    VLAN operational status (``active``, ``suspended``, ``act/lshut``) is recorded
    in ``raw`` rather than promoted to a column: a suspended VLAN still occupies
    its ID, so it is irrelevant to availability, and adding a field would mean a
    migration for information nothing consumes.
    """
    if not output or not output.strip():
        raise DriverParseError("Empty output from 'show vlan brief'.")

    lines = output.splitlines()

    separator_index: int | None = None
    for index, line in enumerate(lines):
        if _SEPARATOR_RE.match(line.strip()) and line.lstrip().startswith("-"):
            separator_index = index
            break

    if separator_index is None:
        # A device that rejected the command ("Invalid input detected") lands here.
        raise DriverParseError(
            "Could not find the column separator in 'show vlan brief' output; "
            "the device may have rejected the command."
        )

    bounds = _column_bounds(lines[separator_index])
    if len(bounds) < 3:
        raise DriverParseError(
            f"'show vlan brief' has {len(bounds)} columns; expected at least 3 "
            "(VLAN, Name, Status)."
        )

    vlan_start = bounds[0][0]
    name_start = bounds[1][0]
    status_start = bounds[2][0]
    ports_start = bounds[3][0] if len(bounds) > 3 else None

    collected: list[dict[str, Any]] = []

    for line in lines[separator_index + 1 :]:
        if not line.strip():
            continue

        vlan_cell = _slice(line, vlan_start, name_start)

        if vlan_cell.isdigit():
            tag = int(vlan_cell)
            collected.append(
                {
                    "vlan_id": tag,
                    "name": _slice(line, name_start, status_start) or None,
                    "status": _slice(line, status_start, ports_start) or None,
                    "ports": (_split_ports(line[ports_start:]) if ports_start is not None else []),
                }
            )
            continue

        # Continuation: no VLAN id in the first column, so the ports belong to the
        # most recent VLAN. Ignore stray lines before any VLAN row.
        if collected and ports_start is not None and line[:ports_start].strip() == "":
            collected[-1]["ports"].extend(_split_ports(line[ports_start:]))

    vlans: list[DiscoveredVlan] = []
    for entry in collected:
        tag = int(entry["vlan_id"])
        name = entry["name"]

        if not MIN_VLAN_ID <= tag <= MAX_VLAN_ID:
            logger.warning("cisco_vlan_id_out_of_range", vlan_id=tag)
            continue
        if name and name.lower() in _DEFAULT_VLAN_NAMES:
            logger.debug("cisco_default_vlan_skipped", vlan_id=tag, name=name)
            continue

        interfaces = _build_interfaces(entry["ports"])
        vlans.append(
            DiscoveredVlan(
                vlan_id=tag,
                name=name,
                description=None,  # IOS has no per-VLAN description in this output
                l3_interface=None,  # filled in by enrich_with_svis
                vxlan_vni=None,
                interfaces=interfaces,
                raw={
                    "source": "show vlan brief",
                    "vlan_id": tag,
                    "status": entry["status"],
                },
            )
        )

    logger.debug("cisco_iosxe_vlans_parsed", count=len(vlans))
    return tuple(vlans)


def _build_interfaces(ports: list[str]) -> tuple[DiscoveredInterface, ...]:
    """Deduplicate port names, preserving order.

    Mode is UNKNOWN throughout: ``show vlan brief`` lists a VLAN's access ports and
    does not reliably distinguish trunk membership. Guessing would be worse than
    saying so — and because the reconciler compares (name, mode) pairs, a
    *consistent* UNKNOWN produces no spurious changes. See docs/decisions.md for
    the trunk-detection follow-up.
    """
    interfaces: list[DiscoveredInterface] = []
    seen: set[str] = set()
    for port in ports:
        key = port.lower()
        if key in seen:
            continue
        seen.add(key)
        interfaces.append(DiscoveredInterface(name=port, mode=InterfaceMode.UNKNOWN))
    return tuple(interfaces)


def parse_svi_interfaces(output: str) -> dict[int, str]:
    """Map VLAN id -> SVI name from ``show ip interface brief``.

    IOS's ``interface Vlan110`` is the equivalent of Junos's ``irb.110``, so this
    populates ``l3_interface`` and keeps the two vendors' records comparable.

    Never raises: a device that refuses this command should still yield VLANs, just
    without L3 information.
    """
    svis: dict[int, str] = {}
    if not output:
        return svis

    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        match = _SVI_RE.match(parts[0])
        if not match:
            continue
        tag = int(match.group(1))
        if MIN_VLAN_ID <= tag <= MAX_VLAN_ID:
            svis[tag] = parts[0]

    logger.debug("cisco_svis_parsed", count=len(svis))
    return svis


def enrich_with_svis(
    vlans: tuple[DiscoveredVlan, ...], svis: dict[int, str]
) -> tuple[DiscoveredVlan, ...]:
    """Attach SVI names to the VLANs that have one."""
    if not svis:
        return vlans

    enriched: list[DiscoveredVlan] = []
    for vlan in vlans:
        svi = svis.get(vlan.vlan_id)
        if svi is None:
            enriched.append(vlan)
            continue
        enriched.append(
            DiscoveredVlan(
                vlan_id=vlan.vlan_id,
                name=vlan.name,
                description=vlan.description,
                l3_interface=svi,
                vxlan_vni=vlan.vxlan_vni,
                interfaces=vlan.interfaces,
                raw={**vlan.raw, "svi": svi},
            )
        )
    return tuple(enriched)


def parse_version(output: str) -> tuple[str | None, str | None]:
    """Extract (model, os_version) from ``show version``. Best effort.

    Never raises — facts are cosmetic, and failing a sync because a banner changed
    format would be absurd.
    """
    model: str | None = None
    version: str | None = None

    if not output:
        return None, None

    # Searched across the whole output, not line by line: the IOS banner wraps, so
    # "Version 16.12.05b" routinely lands on a continuation line that carries none
    # of the keywords a per-line filter would look for. Anchoring on a leading
    # digit is what keeps this from matching the word "Version" elsewhere.
    version_match = re.search(r"Version\s+(\d[^\s,]*)", output)
    if version_match:
        version = version_match.group(1).rstrip(",")

    for line in output.splitlines():
        stripped = line.strip()
        if model is None:
            # "Model Number : WS-C3650-48TS" or "cisco WS-C3650-48TS (MIPS) ..."
            match = re.search(r"[Mm]odel [Nn]umber\s*:\s*(\S+)", stripped)
            if match:
                model = match.group(1)
            elif stripped.lower().startswith("cisco "):
                candidate = stripped.split()[1] if len(stripped.split()) > 1 else ""
                if candidate and candidate.upper() not in {"IOS", "IOS-XE", "ADAPTIVE"}:
                    model = candidate

    return model, version
