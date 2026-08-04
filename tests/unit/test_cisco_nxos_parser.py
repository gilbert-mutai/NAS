"""Cisco NX-OS structured output parsing (Nexus 9000)."""

from __future__ import annotations

import json
import textwrap
from typing import Any

import pytest

from nas.domain.enums import InterfaceMode
from nas.drivers.base import DriverParseError
from nas.drivers.cisco_nxos_parser import (
    enrich,
    parse_show_version,
    parse_show_vlan,
    parse_svi_interfaces,
    parse_vn_segments,
)

VLAN_ROWS: list[dict[str, Any]] = [
    {
        "vlanshowbr-vlanid": "1",
        "vlanshowbr-vlanname": "default",
        "vlanshowbr-vlanstate": "active",
        "vlanshowbr-shutstate": "noshutdown",
        "vlanshowplist-ifidx": "Ethernet1/1,Ethernet1/2",
    },
    {
        "vlanshowbr-vlanid": "110",
        "vlanshowbr-vlanname": "sip-safaricom",
        "vlanshowbr-vlanstate": "active",
        "vlanshowbr-shutstate": "noshutdown",
        # Long port lists arrive as a *list* of strings, each possibly comma-joined.
        "vlanshowplist-ifidx": ["Ethernet1/10-20", "port-channel1,port-channel2"],
    },
    {
        "vlanshowbr-vlanid": "3000",
        "vlanshowbr-vlanname": "vxlan-underlay",
        "vlanshowbr-vlanstate": "active",
        "vlanshowbr-shutstate": "noshutdown",
    },
]

BODY: dict[str, Any] = {"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": VLAN_ROWS}}


def envelope(body: Any, *, code: str = "200", msg: str = "Success") -> dict[str, Any]:
    return {
        "ins_api": {
            "type": "cli_show",
            "version": "1.0",
            "sid": "eoc",
            "outputs": {"output": {"input": "show vlan", "msg": msg, "code": code, "body": body}},
        }
    }


class TestShowVlan:
    def test_parses_the_ins_api_envelope(self) -> None:
        assert [v.vlan_id for v in parse_show_vlan(envelope(BODY))] == [1, 110, 3000]

    def test_parses_a_bare_body(self) -> None:
        assert [v.vlan_id for v in parse_show_vlan(BODY)] == [1, 110, 3000]

    def test_parses_a_json_string(self) -> None:
        assert len(parse_show_vlan(json.dumps(envelope(BODY)))) == 3

    def test_single_vlan_arrives_as_an_object_not_a_list(self) -> None:
        """The classic NX-OS gotcha: code that assumes a list works in the lab and
        breaks on a switch with one VLAN."""
        single = {
            "TABLE_vlanbriefxbrief": {
                "ROW_vlanbriefxbrief": {
                    "vlanshowbr-vlanid": "7",
                    "vlanshowbr-vlanname": "solo",
                    "vlanshowplist-ifidx": "Ethernet1/1",
                }
            }
        }
        vlans = parse_show_vlan(single)
        assert [(v.vlan_id, v.name) for v in vlans] == [(7, "solo")]

    def test_port_lists_are_flattened(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan(BODY)}
        assert [i.name for i in by_tag[110].interfaces] == [
            "Ethernet1/10-20",
            "port-channel1",
            "port-channel2",
        ]

    def test_compressed_ranges_are_left_intact(self) -> None:
        """Expanding Ethernet1/10-20 would invent interface names."""
        by_tag = {v.vlan_id: v for v in parse_show_vlan(BODY)}
        assert "Ethernet1/10-20" in [i.name for i in by_tag[110].interfaces]

    def test_vlan_without_ports(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan(BODY)}
        assert by_tag[3000].interfaces == ()

    def test_mode_is_unknown_not_guessed(self) -> None:
        vlans = parse_show_vlan(BODY)
        assert all(i.mode is InterfaceMode.UNKNOWN for v in vlans for i in v.interfaces)

    def test_state_is_recorded_in_raw(self) -> None:
        by_tag = {v.vlan_id: v for v in parse_show_vlan(BODY)}
        assert by_tag[110].raw["state"] == "active"
        assert by_tag[110].raw["shutstate"] == "noshutdown"

    def test_switch_with_no_vlans_returns_empty_without_raising(self) -> None:
        """Legitimate, and must stay distinguishable from a failure."""
        assert parse_show_vlan(envelope("")) == ()
        assert parse_show_vlan({"some_other_table": {}}) == ()

    def test_duplicate_tags_keep_the_first(self) -> None:
        rows = [
            {"vlanshowbr-vlanid": "5", "vlanshowbr-vlanname": "first"},
            {"vlanshowbr-vlanid": "5", "vlanshowbr-vlanname": "second"},
        ]
        vlans = parse_show_vlan({"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": rows}})
        assert [(v.vlan_id, v.name) for v in vlans] == [(5, "first")]

    def test_out_of_range_tag_is_skipped(self) -> None:
        rows = [
            {"vlanshowbr-vlanid": "9999", "vlanshowbr-vlanname": "bogus"},
            {"vlanshowbr-vlanid": "20", "vlanshowbr-vlanname": "fine"},
        ]
        vlans = parse_show_vlan({"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": rows}})
        assert [v.vlan_id for v in vlans] == [20]

    def test_row_without_an_id_is_skipped(self) -> None:
        rows = [{"vlanshowbr-vlanname": "nameless"}, {"vlanshowbr-vlanid": "20"}]
        vlans = parse_show_vlan({"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": rows}})
        assert [v.vlan_id for v in vlans] == [20]

    def test_falls_back_to_the_utf_id_field(self) -> None:
        rows = [{"vlanshowbr-vlanid-utf": "42", "vlanshowbr-vlanname": "utf"}]
        vlans = parse_show_vlan({"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": rows}})
        assert [v.vlan_id for v in vlans] == [42]


class TestFailuresMustRaise:
    """A rejected command must never be mistaken for "this switch has no VLANs" —
    that is the difference between a failed read and mass deletion."""

    def test_nxapi_error_code_raises(self) -> None:
        with pytest.raises(DriverParseError, match="rejected the command"):
            parse_show_vlan(envelope(None, code="400", msg="Invalid command"))

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(DriverParseError, match="not valid JSON"):
            parse_show_vlan("not json at all")

    def test_non_object_payload_raises(self) -> None:
        with pytest.raises(DriverParseError, match="not a JSON object"):
            parse_show_vlan(["a", "list"])

    def test_envelope_without_outputs_raises(self) -> None:
        with pytest.raises(DriverParseError, match="no outputs"):
            parse_show_vlan({"ins_api": {"version": "1.0"}})

    def test_non_object_body_raises(self) -> None:
        with pytest.raises(DriverParseError, match="not a JSON object"):
            parse_show_vlan(envelope("some ascii text"))


class TestVnSegments:
    RUNNING_CONFIG = textwrap.dedent(
        """
        vlan 1
        vlan 110
          name sip-safaricom
          vn-segment 10110
        vlan 3000
          name vxlan-underlay
          vn-segment 13000
        vlan 120
          name no-vni
        interface Ethernet1/1
          vn-segment 99999
        """
    )

    def test_extracts_vn_segments(self) -> None:
        assert parse_vn_segments(self.RUNNING_CONFIG) == {110: 10110, 3000: 13000}

    def test_vlan_without_a_vn_segment_is_absent(self) -> None:
        assert 120 not in parse_vn_segments(self.RUNNING_CONFIG)

    def test_vn_segment_outside_a_vlan_block_is_ignored(self) -> None:
        """The interface block also contains 'vn-segment'."""
        assert 99999 not in parse_vn_segments(self.RUNNING_CONFIG).values()

    def test_empty_input_is_not_an_error(self) -> None:
        """VXLAN may not be configured, or the account may lack the privilege."""
        assert parse_vn_segments("") == {}


class TestSviParsing:
    def test_finds_svis_at_any_nesting_depth(self) -> None:
        payload = {
            "TABLE_interface": {
                "ROW_interface": [
                    {"interface": "Vlan110"},
                    {"interface": "Ethernet1/1"},
                    {"interface": "Vlan3000"},
                ]
            }
        }
        assert parse_svi_interfaces(payload) == {110: "Vlan110", 3000: "Vlan3000"}

    def test_malformed_payload_is_not_an_error(self) -> None:
        assert parse_svi_interfaces("garbage") == {}
        assert parse_svi_interfaces(None) == {}


class TestEnrich:
    def test_attaches_vnis_and_svis(self) -> None:
        vlans = parse_show_vlan(BODY)
        enriched = enrich(vlans, vnis={110: 10110}, svis={110: "Vlan110"})
        by_tag = {v.vlan_id: v for v in enriched}
        assert by_tag[110].vxlan_vni == 10110
        assert by_tag[110].l3_interface == "Vlan110"
        assert by_tag[1].vxlan_vni is None

    def test_records_enrichment_in_raw(self) -> None:
        enriched = enrich(parse_show_vlan(BODY), vnis={110: 10110})
        by_tag = {v.vlan_id: v for v in enriched}
        assert by_tag[110].raw["vn_segment"] == 10110
        assert by_tag[110].raw["source"] == "show vlan | json"

    def test_nothing_to_add_returns_the_input(self) -> None:
        vlans = parse_show_vlan(BODY)
        assert enrich(vlans) is vlans


class TestShowVersion:
    def test_extracts_model_and_version(self) -> None:
        payload = {
            "chassis_id": "Nexus9000 C93180YC-EX chassis",
            "nxos_ver_str": "9.3(10)",
            "host_name": "n9k-1",
        }
        assert parse_show_version(payload) == ("Nexus9000 C93180YC-EX chassis", "9.3(10)")

    def test_falls_back_to_kickstart_version(self) -> None:
        payload = {"chassis_id": "Nexus9000", "kickstart_ver_str": "7.0(3)I7(6)"}
        assert parse_show_version(payload)[1] == "7.0(3)I7(6)"

    def test_missing_information_is_not_an_error(self) -> None:
        assert parse_show_version({}) == (None, None)
        assert parse_show_version("garbage") == (None, None)
