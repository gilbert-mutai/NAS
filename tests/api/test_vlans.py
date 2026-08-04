"""VLAN endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from nas.domain.entities import VlanInterface
from nas.domain.enums import InterfaceMode, VlanState
from tests.fakes import InMemoryVlanRepository, make_vlan

PATH = "/api/v1/vlans"


class TestSearchVlans:
    async def test_returns_data_and_pagination(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(PATH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"data", "pagination"}
        assert body["pagination"]["total"] == 1

    async def test_payload_fields(self, auth_client: AsyncClient) -> None:
        item = (await auth_client.get(PATH)).json()["data"][0]
        assert item["vlan_id"] == 100
        assert item["name"] == "sip-angani"
        assert item["state"] == "active"
        assert item["switch_name"] == "adc-core-sw1"
        assert item["switch_site"] == "ADC NBO"
        # The record id and the 802.1Q tag are distinct concepts and both exposed.
        assert item["id"] == 1
        assert "interface_count" in item
        assert {"first_seen_at", "last_seen_at", "last_synced_at"} <= set(item)

    async def test_interfaces_are_included(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(
            make_vlan(
                record_id=2,
                vlan_id=200,
                interfaces=(
                    VlanInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),
                    VlanInterface(name="ge-0/0/2", mode=InterfaceMode.ACCESS),
                ),
            )
        )
        item = next(v for v in (await auth_client.get(PATH)).json()["data"] if v["vlan_id"] == 200)
        assert item["interface_count"] == 2
        assert {i["name"]: i["mode"] for i in item["interfaces"]} == {
            "ge-0/0/1": "trunk",
            "ge-0/0/2": "access",
        }

    async def test_filters_by_tag(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(make_vlan(record_id=2, vlan_id=200))
        body = (await auth_client.get(PATH, params={"vlan_id": 200})).json()
        assert [v["vlan_id"] for v in body["data"]] == [200]

    async def test_filters_by_state(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(make_vlan(record_id=2, vlan_id=200, state=VlanState.MISSING))
        body = (await auth_client.get(PATH, params={"state": "missing"})).json()
        assert [v["vlan_id"] for v in body["data"]] == [200]

    async def test_filters_by_switch(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(make_vlan(record_id=2, vlan_id=200, switch_id=9))
        body = (await auth_client.get(PATH, params={"switch_id": 9})).json()
        assert [v["vlan_id"] for v in body["data"]] == [200]

    async def test_free_text_search(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(
            make_vlan(record_id=2, vlan_id=200, name="sip-safaricom", description="Provider trunk")
        )
        body = (await auth_client.get(PATH, params={"q": "safaricom"})).json()
        assert [v["vlan_id"] for v in body["data"]] == [200]

    @pytest.mark.parametrize("bad", [0, 4095, 5000, -1])
    async def test_out_of_range_tag_filter_is_rejected(
        self, auth_client: AsyncClient, bad: int
    ) -> None:
        assert (await auth_client.get(PATH, params={"vlan_id": bad})).status_code == 422

    async def test_unknown_state_is_rejected(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(PATH, params={"state": "wobbly"})).status_code == 422

    async def test_requires_vlans_read_scope(self, client: AsyncClient) -> None:
        assert (await client.get(PATH)).status_code == 401


class TestGetVlan:
    async def test_returns_the_record_unwrapped(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(f"{PATH}/1")
        assert response.status_code == 200
        assert response.json()["id"] == 1
        assert "data" not in response.json()

    async def test_unknown_record_returns_404_envelope(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(f"{PATH}/4242")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND"
        assert error["details"]["vlan_record_id"] == 4242

    async def test_zero_id_is_rejected(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(f"{PATH}/0")).status_code == 422


class TestLookup:
    async def test_in_use_tag(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(make_vlan(record_id=2, vlan_id=1234, switch_id=2, switch_name="sw-b"))
        body = (await auth_client.get(f"{PATH}/lookup/1234")).json()
        assert body["availability"] == "in_use"
        assert body["is_available"] is False
        assert body["switch_count"] == 1
        assert [u["switch_name"] for u in body["active_usages"]] == ["sw-b"]

    async def test_available_tag(self, auth_client: AsyncClient) -> None:
        body = (await auth_client.get(f"{PATH}/lookup/999")).json()
        assert body["availability"] == "available"
        assert body["is_available"] is True
        assert body["switch_count"] == 0
        assert body["active_usages"] == []

    async def test_aggregates_across_switches(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        for index, name in enumerate(("sw-a", "sw-b", "sw-c"), start=10):
            vlan_repository.seed(
                make_vlan(record_id=index, vlan_id=1234, switch_id=index, switch_name=name)
            )
        body = (await auth_client.get(f"{PATH}/lookup/1234")).json()
        assert body["switch_count"] == 3
        assert sorted(u["switch_name"] for u in body["active_usages"]) == ["sw-a", "sw-b", "sw-c"]

    async def test_missing_records_appear_as_historic(
        self, auth_client: AsyncClient, vlan_repository: InMemoryVlanRepository
    ) -> None:
        vlan_repository.seed(
            make_vlan(record_id=2, vlan_id=1234, state=VlanState.MISSING, switch_name="sw-old")
        )
        body = (await auth_client.get(f"{PATH}/lookup/1234")).json()
        assert body["availability"] == "available"
        assert [u["switch_name"] for u in body["historic_usages"]] == ["sw-old"]

    @pytest.mark.parametrize("tag", [0, 4095])
    async def test_reserved_tags(self, auth_client: AsyncClient, tag: int) -> None:
        body = (await auth_client.get(f"{PATH}/lookup/{tag}")).json()
        assert body["availability"] == "reserved"
        assert body["is_available"] is False

    @pytest.mark.parametrize("tag", [4096, 99999, -1])
    async def test_out_of_range_tags_rejected(self, auth_client: AsyncClient, tag: int) -> None:
        assert (await auth_client.get(f"{PATH}/lookup/{tag}")).status_code == 422

    async def test_staleness_is_reported(self, auth_client: AsyncClient) -> None:
        """The CRM must be able to warn before trusting an 'available' verdict."""
        body = (await auth_client.get(f"{PATH}/lookup/999")).json()
        assert "is_stale" in body
        assert "data_as_of" in body

    async def test_never_synced_is_stale(self, auth_client: AsyncClient) -> None:
        # The default sync-run fake has no runs recorded.
        body = (await auth_client.get(f"{PATH}/lookup/999")).json()
        assert body["is_stale"] is True
        assert body["data_as_of"] is None

    async def test_lookup_route_is_not_shadowed_by_the_id_route(
        self, auth_client: AsyncClient
    ) -> None:
        """`/vlans/lookup/1234` must not be parsed as `/vlans/{vlan_record_id}`."""
        response = await auth_client.get(f"{PATH}/lookup/1234")
        assert response.status_code == 200
        assert "availability" in response.json()
