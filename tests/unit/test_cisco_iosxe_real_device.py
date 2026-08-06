"""Parsing output recorded from a production Catalyst 3650.

`switch-01.westpoint` — WS-C3650-48PD, IOS-XE 16.6.9, member 3 of a stack, 69
customer VLANs. Everything else in the Cisco suite uses fixtures I wrote; this uses
output the device actually produced, which is a different kind of evidence.

The fixture reproduces IOS's exact column geometry (vlan@0 w4, name@5 w32,
status@38 w9, ports@48) and VLAN 1's real 13-line port wrap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nas.drivers.cisco_iosxe_parser import parse_show_vlan_brief

FIXTURE = Path(__file__).parent.parent / "fixtures" / "switch01_westpoint_show_vlan_brief.txt"

EXPECTED_VLAN_COUNT = 69
IOS_DEFAULT_TAGS = (1002, 1003, 1004, 1005)


@pytest.fixture(scope="module")
def parsed() -> dict[int, object]:
    return {v.vlan_id: v for v in parse_show_vlan_brief(FIXTURE.read_text())}


class TestRealSwitchOutput:
    def test_vlan_count(self, parsed: dict[int, object]) -> None:
        """73 rows on the device, minus the 4 IOS-created defaults."""
        assert len(parsed) == EXPECTED_VLAN_COUNT

    def test_ios_default_vlans_are_excluded(self, parsed: dict[int, object]) -> None:
        for tag in IOS_DEFAULT_TAGS:
            assert tag not in parsed

    def test_thirteen_line_port_wrap_is_reassembled(self, parsed: dict[int, object]) -> None:
        """VLAN 1 carries 52 ports across 13 continuation lines — the single most
        error-prone thing in this output format."""
        ports = [i.name for i in parsed[1].interfaces]  # type: ignore[attr-defined]
        assert len(ports) == 52
        assert all(f"Gi3/0/{n}" in ports for n in range(1, 49))
        assert ports[-4:] == ["Gi3/1/1", "Gi3/1/2", "Te3/1/3", "Te3/1/4"]

    def test_continuation_does_not_leak_into_the_next_vlan(self, parsed: dict[int, object]) -> None:
        assert parsed[2].interfaces == ()  # type: ignore[attr-defined]
        assert parsed[2].name == "VLAN0002"  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("tag", "name"),
        [
            (95, "VLAN0095-LDAP"),  # digits then a hyphenated suffix
            (112, "TechUPSMon"),
            (201, "MX-BRIDGE"),
            (363, "prod.js_db-svrs"),  # dots and underscores
            (700, "Colt-MPLS"),
            (901, "New-EUC-Workstations"),
        ],
    )
    def test_real_vlan_names(self, parsed: dict[int, object], tag: int, name: str) -> None:
        assert parsed[tag].name == name  # type: ignore[attr-defined]

    def test_stack_member_interface_naming(self, parsed: dict[int, object]) -> None:
        """A stacked 3650 reports Gi3/0/x, not Gi1/0/x."""
        ports = [i.name for i in parsed[1].interfaces]  # type: ignore[attr-defined]
        assert all(p.startswith(("Gi3/", "Te3/")) for p in ports)

    def test_vlans_without_access_ports_parse_cleanly(self, parsed: dict[int, object]) -> None:
        """68 of 69 VLANs are trunk-carried, so their Ports column is blank.
        Blank must mean "no access ports", not a parse failure."""
        empty = [t for t, v in parsed.items() if not v.interfaces]  # type: ignore[attr-defined]
        assert len(empty) == EXPECTED_VLAN_COUNT - 1

    def test_all_tags_assignable(self, parsed: dict[int, object]) -> None:
        assert all(1 <= tag <= 4094 for tag in parsed)

    def test_status_is_captured_in_raw(self, parsed: dict[int, object]) -> None:
        assert parsed[1].raw["status"] == "active"  # type: ignore[attr-defined]
