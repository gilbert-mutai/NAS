"""Cisco IOS / IOS-XE ``show`` output parsing.

Fixtures reproduce real Catalyst output. This is how the 3650 integration is
verified without a switch and without netmiko installed — the parser is pure.
"""

from __future__ import annotations

import textwrap

import pytest

from nas.domain.enums import InterfaceMode
from nas.drivers.base import DriverParseError
from nas.drivers.cisco_iosxe_parser import (
    enrich_with_svis,
    parse_show_vlan_brief,
    parse_svi_interfaces,
    parse_version,
)

# Catalyst 3650. Note VLAN 110's ports wrapping onto a continuation line, the
# IOS-created 1002/1003 defaults, and the act/lshut + suspended states.
VLAN_BRIEF = textwrap.dedent(
    """
    VLAN Name                             Status    Ports
    ---- -------------------------------- --------- -------------------------------
    1    default                          active    Gi1/0/1, Gi1/0/2, Gi1/0/3
    10   mgmt                             active    Gi1/0/47, Gi1/0/48
    110  sip-safaricom                    active    Gi1/0/12, Gi1/0/13, Gi1/0/14
                                                    Gi1/0/15, Gi1/0/16
    1001 cust-acme-voice                  act/lshut Gi1/0/20
    2001 cust-epsilon-voice               suspended
    1002 fddi-default                     act/unsup
    1003 token-ring-default               act/unsup
    """
).strip("\n")

IP_INT_BRIEF = textwrap.dedent(
    """
    Interface              IP-Address      OK? Method Status                Protocol
    Vlan1                  unassigned      YES NVRAM  administratively down down
    Vlan110                10.20.110.1     YES NVRAM  up                    up
    GigabitEthernet1/0/1   unassigned      YES unset  up                    up
    Loopback0              10.255.0.1      YES NVRAM  up                    up
    """
).strip("\n")


class TestShowVlanBrief:
    def test_parses_expected_vlans(self) -> None:
        tags = [v.vlan_id for v in parse_show_vlan_brief(VLAN_BRIEF)]
        assert tags == [1, 10, 110, 1001, 2001]

    def test_ios_default_vlans_are_excluded(self) -> None:
        """1002-1005 are IOS artefacts, not inventory."""
        tags = {v.vlan_id for v in parse_show_vlan_brief(VLAN_BRIEF)}
        assert 1002 not in tags
        assert 1003 not in tags

    def test_names_are_extracted(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan_brief(VLAN_BRIEF)}
        assert by_tag[110].name == "sip-safaricom"
        assert by_tag[1001].name == "cust-acme-voice"

    def test_wrapped_ports_are_joined_to_the_right_vlan(self) -> None:
        """The single most error-prone part of this output."""
        by_tag = {v.vlan_id: v for v in parse_show_vlan_brief(VLAN_BRIEF)}
        assert [i.name for i in by_tag[110].interfaces] == [
            "Gi1/0/12",
            "Gi1/0/13",
            "Gi1/0/14",
            "Gi1/0/15",
            "Gi1/0/16",
        ]
        # And the continuation must not leak into the next VLAN.
        assert [i.name for i in by_tag[1001].interfaces] == ["Gi1/0/20"]

    def test_vlan_with_no_ports(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan_brief(VLAN_BRIEF)}
        assert by_tag[2001].interfaces == ()

    def test_suspended_vlan_is_kept(self) -> None:
        """It still occupies the ID, so it must not read as available."""
        assert 2001 in {v.vlan_id for v in parse_show_vlan_brief(VLAN_BRIEF)}

    def test_status_is_recorded_in_raw(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan_brief(VLAN_BRIEF)}
        assert by_tag[2001].raw["status"] == "suspended"
        assert by_tag[1001].raw["status"] == "act/lshut"

    def test_mode_is_unknown_not_guessed(self) -> None:
        """'show vlan brief' cannot distinguish access from trunk. A consistent
        UNKNOWN produces no spurious reconciliation changes; a guess would."""
        vlans = parse_show_vlan_brief(VLAN_BRIEF)
        assert all(i.mode is InterfaceMode.UNKNOWN for v in vlans for i in v.interfaces)

    def test_narrow_columns_are_handled(self) -> None:
        """Column geometry comes from the separator row, not fixed offsets."""
        narrow = (
            "VLAN Name        Status    Ports\n"
            "---- ----------- --------- ----------\n"
            "5    tiny        active    Gi0/1\n"
        )
        vlans = parse_show_vlan_brief(narrow)
        assert [(v.vlan_id, v.name) for v in vlans] == [(5, "tiny")]
        assert [i.name for i in vlans[0].interfaces] == ["Gi0/1"]

    def test_output_without_a_ports_column(self) -> None:
        minimal = "VLAN Name     Status\n---- -------- ---------\n7    seven    active\n"
        assert [v.vlan_id for v in parse_show_vlan_brief(minimal)] == [7]

    def test_out_of_range_tag_is_skipped(self) -> None:
        weird = (
            "VLAN Name     Status    Ports\n"
            "---- -------- --------- -----\n"
            "9999 bogus    active    Gi0/1\n"
            "20   fine     active    Gi0/2\n"
        )
        assert [v.vlan_id for v in parse_show_vlan_brief(weird)] == [20]

    @pytest.mark.parametrize("payload", ["", "   ", "\n\n"])
    def test_empty_output_raises(self, payload: str) -> None:
        with pytest.raises(DriverParseError, match="Empty output"):
            parse_show_vlan_brief(payload)

    def test_rejected_command_raises_rather_than_reporting_no_vlans(self) -> None:
        """Critical: returning () here would look to the reconciler like every
        VLAN had been deleted."""
        with pytest.raises(DriverParseError, match="separator"):
            parse_show_vlan_brief("% Invalid input detected at '^' marker.")

    def test_too_few_columns_raises(self) -> None:
        with pytest.raises(DriverParseError, match="columns"):
            parse_show_vlan_brief("VLAN Name\n---- ----\n1    default\n")


class TestSviParsing:
    def test_finds_svis(self) -> None:
        assert parse_svi_interfaces(IP_INT_BRIEF) == {1: "Vlan1", 110: "Vlan110"}

    def test_ignores_physical_and_loopback_interfaces(self) -> None:
        svis = parse_svi_interfaces(IP_INT_BRIEF)
        assert all(name.lower().startswith("vlan") for name in svis.values())

    def test_empty_output_is_not_an_error(self) -> None:
        """SVI lookup is enrichment; a device refusing it must not fail the sync."""
        assert parse_svi_interfaces("") == {}

    def test_garbage_is_not_an_error(self) -> None:
        assert parse_svi_interfaces("% Invalid input") == {}


class TestEnrichment:
    def test_attaches_svi_as_l3_interface(self) -> None:
        vlans = parse_show_vlan_brief(VLAN_BRIEF)
        enriched = enrich_with_svis(vlans, parse_svi_interfaces(IP_INT_BRIEF))
        by_tag = {v.vlan_id: v for v in enriched}
        assert by_tag[110].l3_interface == "Vlan110"
        assert by_tag[10].l3_interface is None

    def test_preserves_everything_else(self) -> None:
        vlans = parse_show_vlan_brief(VLAN_BRIEF)
        enriched = enrich_with_svis(vlans, {110: "Vlan110"})
        original = {v.vlan_id: v for v in vlans}[110]
        updated = {v.vlan_id: v for v in enriched}[110]
        assert updated.name == original.name
        assert updated.interfaces == original.interfaces

    def test_no_svis_returns_the_input_unchanged(self) -> None:
        vlans = parse_show_vlan_brief(VLAN_BRIEF)
        assert enrich_with_svis(vlans, {}) is vlans


# Recorded verbatim from switch-01.westpoint, a real WS-C3650-48PD running
# IOS-XE 16.6.9. Trimmed of legal boilerplate only; every line that matters to the
# parser is untouched. Fixtures built by hand are a guess — this is evidence.
REAL_3650_VERSION = textwrap.dedent(
    """
    Cisco IOS XE Software, Version 16.06.09
    Cisco IOS Software [Everest], Catalyst L3 Switch Software (CAT3K_CAA-UNIVERSALK9-M), Version 16.6.9, RELEASE SOFTWARE (fc3)
    Technical Support: http://www.cisco.com/techsupport
    Copyright (c) 1986-2021 by Cisco Systems, Inc.
    Compiled Wed 24-Feb-21 07:17 by mcpre

    Cisco IOS-XE software, Copyright (c) 2005-2021 by cisco Systems, Inc.

    ROM: IOS-XE ROMMON
    BOOTLDR: CAT3K_CAA Boot Loader (CAT3K_CAA-HBOOT-M) Version 4.66, RELEASE SOFTWARE (P)

    switch-01.westpoint uptime is 1 day, 2 hours, 28 minutes
    System image file is "flash:packages.conf"

    Technology Package License Information:
    Technology-package                   Technology-package
    Current             Type             Next reboot
    lanbasek9           Permanent        lanbasek9

    cisco WS-C3650-48PD (MIPS) processor (revision Q0) with 852817K/6147K bytes of memory.
    Processor board ID FDO2140V04X
    50 Gigabit Ethernet interfaces

    Base Ethernet MAC Address          : 6c:b2:ae:9d:c2:80
    Motherboard Assembly Number        : 73-15897-06
    Motherboard Serial Number          : FDO21392QVG
    Model Revision Number              : Q0
    Motherboard Revision Number        : A0
    Model Number                       : WS-C3650-48PD
    System Serial Number               : FDO2140V04X

    Switch Ports Model              SW Version        SW Image              Mode
    *    3 52    WS-C3650-48PD      16.6.9            CAT3K_CAA-UNIVERSALK9 INSTALL

    Configuration register is 0x102
    """
).strip("\n")


class TestRealDeviceVersion:
    """Against output recorded from an actual WS-C3650-48PD (IOS-XE 16.6.9)."""

    def test_extracts_model_and_version(self) -> None:
        assert parse_version(REAL_3650_VERSION) == ("WS-C3650-48PD", "16.06.09")

    def test_does_not_mistake_the_bootloader_version(self) -> None:
        """'Boot Loader ... Version 4.66' appears before the model lines."""
        assert parse_version(REAL_3650_VERSION)[1] != "4.66"

    def test_does_not_mistake_model_revision_for_the_model(self) -> None:
        """'Model Revision Number : Q0' is a near-miss for the Model Number regex."""
        assert parse_version(REAL_3650_VERSION)[0] != "Q0"

    def test_does_not_mistake_the_software_banner_for_the_model(self) -> None:
        """Three lines begin with 'Cisco IOS...', which the fallback would grab."""
        model = parse_version(REAL_3650_VERSION)[0]
        assert model is not None
        assert not model.upper().startswith("IOS")


class TestVersionParsing:
    def test_extracts_model_and_version(self) -> None:
        output = textwrap.dedent(
            """
            Cisco IOS Software, IOS-XE Software, Catalyst L3 Switch Software
            (CAT3K_CAA-UNIVERSALK9-M), Version 16.12.05b, RELEASE SOFTWARE (fc1)
            Model Number                         : WS-C3650-48TS
            """
        )
        model, version = parse_version(output)
        assert model == "WS-C3650-48TS"
        assert version == "16.12.05b"

    def test_missing_information_is_not_an_error(self) -> None:
        """Facts are cosmetic — never fail a sync over a banner format change."""
        assert parse_version("") == (None, None)
        assert parse_version("something unexpected") == (None, None)
