"""Junos ``get-vlan-information`` parsing.

Fixtures reproduce the two schema shapes real hardware returns. This is the only
way to test the Juniper integration without a switch — and it runs with
`junos-eznc` absent, because the parser deliberately imports no PyEZ.
"""

from __future__ import annotations

import textwrap

import pytest

from nas.domain.enums import InterfaceMode
from nas.drivers.base import DriverParseError
from nas.drivers.juniper_parser import parse_vlan_information

# EX4300 / QFX and newer.
ELS_REPLY = textwrap.dedent(
    """
    <rpc-reply xmlns:junos="http://xml.juniper.net/junos/18.4R3/junos">
      <l2ng-l2ald-vlan-instance-information>
        <l2ng-l2ald-vlan-instance-group>
          <l2ng-l2rtb-vlan-name>sip-safaricom</l2ng-l2rtb-vlan-name>
          <l2ng-l2rtb-vlan-tag>110</l2ng-l2rtb-vlan-tag>
          <l2ng-l2rtb-vlan-l3-interface>irb.110</l2ng-l2rtb-vlan-l3-interface>
          <l2ng-l2rtb-vlan-member>
            <l2ng-l2rtb-vlan-member-interface>ge-0/0/12.0</l2ng-l2rtb-vlan-member-interface>
            <l2ng-l2rtb-vlan-member-tagness>tagged</l2ng-l2rtb-vlan-member-tagness>
          </l2ng-l2rtb-vlan-member>
          <l2ng-l2rtb-vlan-member>
            <l2ng-l2rtb-vlan-member-interface>ae0.0</l2ng-l2rtb-vlan-member-interface>
            <l2ng-l2rtb-vlan-member-tagness>untagged</l2ng-l2rtb-vlan-member-tagness>
          </l2ng-l2rtb-vlan-member>
        </l2ng-l2ald-vlan-instance-group>
        <l2ng-l2ald-vlan-instance-group>
          <l2ng-l2rtb-vlan-name>vxlan-underlay</l2ng-l2rtb-vlan-name>
          <l2ng-l2rtb-vlan-tag>3000</l2ng-l2rtb-vlan-tag>
          <l2ng-l2rtb-vlan-vxlan-vni>13000</l2ng-l2rtb-vlan-vxlan-vni>
        </l2ng-l2ald-vlan-instance-group>
      </l2ng-l2ald-vlan-instance-information>
    </rpc-reply>
    """
).strip()

# EX2200 / EX3300 and older.
LEGACY_REPLY = textwrap.dedent(
    """
    <rpc-reply>
      <vlan-information>
        <vlan>
          <vlan-name>sip-angani</vlan-name>
          <vlan-tag>100</vlan-tag>
          <vlan-l3-interface>vlan.100</vlan-l3-interface>
          <vlan-interfaces>
            <vlan-interface><vlan-interface-name>ge-0/0/1.0</vlan-interface-name></vlan-interface>
            <vlan-interface><vlan-interface-name>ge-0/0/2.0</vlan-interface-name></vlan-interface>
          </vlan-interfaces>
        </vlan>
      </vlan-information>
    </rpc-reply>
    """
).strip()


class TestElsSchema:
    def test_parses_all_vlans(self) -> None:
        vlans = parse_vlan_information(ELS_REPLY)
        assert sorted(v.vlan_id for v in vlans) == [110, 3000]

    def test_extracts_scalar_fields(self) -> None:
        vlan = next(v for v in parse_vlan_information(ELS_REPLY) if v.vlan_id == 110)
        assert vlan.name == "sip-safaricom"
        assert vlan.l3_interface == "irb.110"

    def test_extracts_vxlan_vni(self) -> None:
        vlan = next(v for v in parse_vlan_information(ELS_REPLY) if v.vlan_id == 3000)
        assert vlan.vxlan_vni == 13000

    def test_maps_tagness_to_interface_mode(self) -> None:
        vlan = next(v for v in parse_vlan_information(ELS_REPLY) if v.vlan_id == 110)
        modes = {i.name: i.mode for i in vlan.interfaces}
        assert modes == {
            "ge-0/0/12.0": InterfaceMode.TRUNK,
            "ae0.0": InterfaceMode.ACCESS,
        }

    def test_vlan_without_members_parses(self) -> None:
        vlan = next(v for v in parse_vlan_information(ELS_REPLY) if v.vlan_id == 3000)
        assert vlan.interfaces == ()

    def test_records_the_schema_it_matched(self) -> None:
        vlan = parse_vlan_information(ELS_REPLY)[0]
        assert vlan.raw["schema"] == "l2ng-l2ald-vlan-instance-group"


class TestLegacySchema:
    def test_parses_vlan(self) -> None:
        vlans = parse_vlan_information(LEGACY_REPLY)
        assert len(vlans) == 1
        assert vlans[0].vlan_id == 100
        assert vlans[0].name == "sip-angani"
        assert vlans[0].l3_interface == "vlan.100"

    def test_parses_members_without_tagness(self) -> None:
        """Older Junos omits tagness; mode must degrade to unknown, not fail."""
        interfaces = parse_vlan_information(LEGACY_REPLY)[0].interfaces
        assert [i.name for i in interfaces] == ["ge-0/0/1.0", "ge-0/0/2.0"]
        assert all(i.mode is InterfaceMode.UNKNOWN for i in interfaces)


class TestNamespaceHandling:
    def test_namespaced_elements_are_matched_by_local_name(self) -> None:
        namespaced = (
            '<rpc-reply xmlns="http://xml.juniper.net/junos"><vlan-information>'
            "<vlan><vlan-name>ns-vlan</vlan-name><vlan-tag>42</vlan-tag></vlan>"
            "</vlan-information></rpc-reply>"
        )
        vlans = parse_vlan_information(namespaced)
        assert [v.vlan_id for v in vlans] == [42]


class TestResilience:
    def test_vlan_without_a_tag_is_skipped_not_fatal(self) -> None:
        """One odd entry must not fail the whole sync — which would leave the
        switch unreadable and freeze its data."""
        reply = (
            "<rpc-reply><vlan-information>"
            "<vlan><vlan-name>no-tag</vlan-name></vlan>"
            "<vlan><vlan-name>fine</vlan-name><vlan-tag>50</vlan-tag></vlan>"
            "</vlan-information></rpc-reply>"
        )
        assert [v.vlan_id for v in parse_vlan_information(reply)] == [50]

    def test_out_of_range_tag_is_skipped(self) -> None:
        reply = (
            "<rpc-reply><vlan-information>"
            "<vlan><vlan-name>bad</vlan-name><vlan-tag>9999</vlan-tag></vlan>"
            "<vlan><vlan-name>ok</vlan-name><vlan-tag>60</vlan-tag></vlan>"
            "</vlan-information></rpc-reply>"
        )
        assert [v.vlan_id for v in parse_vlan_information(reply)] == [60]

    def test_non_numeric_tag_is_skipped(self) -> None:
        reply = (
            "<rpc-reply><vlan-information>"
            "<vlan><vlan-name>bad</vlan-name><vlan-tag>abc</vlan-tag></vlan>"
            "</vlan-information></rpc-reply>"
        )
        assert parse_vlan_information(reply) == ()

    def test_duplicate_tag_keeps_the_first(self) -> None:
        """Junos can list a VLAN once per routing instance."""
        reply = (
            "<rpc-reply><vlan-information>"
            "<vlan><vlan-name>first</vlan-name><vlan-tag>70</vlan-tag></vlan>"
            "<vlan><vlan-name>second</vlan-name><vlan-tag>70</vlan-tag></vlan>"
            "</vlan-information></rpc-reply>"
        )
        vlans = parse_vlan_information(reply)
        assert len(vlans) == 1
        assert vlans[0].name == "first"

    def test_duplicate_member_interface_is_deduplicated(self) -> None:
        reply = (
            "<rpc-reply><vlan-information><vlan>"
            "<vlan-name>v</vlan-name><vlan-tag>80</vlan-tag>"
            "<vlan-interfaces>"
            "<vlan-interface><vlan-interface-name>ge-0/0/1.0</vlan-interface-name></vlan-interface>"
            "<vlan-interface><vlan-interface-name>ge-0/0/1.0</vlan-interface-name></vlan-interface>"
            "</vlan-interfaces></vlan></vlan-information></rpc-reply>"
        )
        assert len(parse_vlan_information(reply)[0].interfaces) == 1

    def test_switch_with_no_vlans_is_not_an_error(self) -> None:
        """A device legitimately may have none, which is different from a failure
        to read it. Returning empty (not raising) keeps that distinction."""
        assert parse_vlan_information("<rpc-reply><vlan-information/></rpc-reply>") == ()


class TestMalformedInput:
    @pytest.mark.parametrize("payload", ["", "   ", "\n"])
    def test_empty_reply_raises(self, payload: str) -> None:
        with pytest.raises(DriverParseError, match="Empty reply"):
            parse_vlan_information(payload)

    def test_unparseable_xml_raises(self) -> None:
        with pytest.raises(DriverParseError, match="not well-formed"):
            parse_vlan_information("<rpc-reply><unclosed>")

    def test_xxe_entity_expansion_is_blocked(self) -> None:
        """Device output is untrusted input; stdlib ElementTree would expand this."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            "<rpc-reply><vlan-information><vlan>"
            "<vlan-name>&x;</vlan-name><vlan-tag>1</vlan-tag>"
            "</vlan></vlan-information></rpc-reply>"
        )
        with pytest.raises(DriverParseError):
            parse_vlan_information(payload)

    def test_billion_laughs_is_blocked(self) -> None:
        payload = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE lolz ["
            '<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
            "]>"
            "<rpc-reply><vlan-information><vlan>"
            "<vlan-name>&lol3;</vlan-name><vlan-tag>1</vlan-tag>"
            "</vlan></vlan-information></rpc-reply>"
        )
        with pytest.raises(DriverParseError):
            parse_vlan_information(payload)
