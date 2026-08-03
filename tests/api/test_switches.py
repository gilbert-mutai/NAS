"""Switch inventory endpoints."""

from __future__ import annotations

from httpx import AsyncClient

from nas.domain.enums import Vendor
from tests.fakes import InMemorySwitchRepository, make_switch

PATH = "/api/v1/switches"


class TestListSwitches:
    async def test_returns_data_and_pagination(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(PATH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"data", "pagination"}
        assert body["pagination"] == {
            "page": 1,
            "page_size": 50,
            "total": 1,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
        }

    async def test_switch_payload_fields(self, auth_client: AsyncClient) -> None:
        item = (await auth_client.get(PATH)).json()["data"][0]
        assert item["name"] == "adc-core-sw1"
        assert item["hostname"] == "10.20.0.11"
        assert item["vendor"] == "juniper"
        assert item["vendor_label"] == "Juniper"
        assert item["credential_status"] == "resolved"
        assert item["reachability"] == "unknown"

    async def test_payload_contains_no_secret_fields(self, auth_client: AsyncClient) -> None:
        """The API must expose the credential *name* and nothing more."""
        item = (await auth_client.get(PATH)).json()["data"][0]
        assert item["credential_ref"] == "juniper-core"
        forbidden = {"password", "ssh_password", "private_key", "private_key_path", "secret"}
        assert forbidden.isdisjoint(item)

    async def test_filters_by_vendor(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(make_switch(switch_id=2, name="cisco-sw", vendor=Vendor.CISCO))
        body = (await auth_client.get(PATH, params={"vendor": "cisco"})).json()
        assert [item["name"] for item in body["data"]] == ["cisco-sw"]

    async def test_rejects_unknown_vendor(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(PATH, params={"vendor": "acme"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_filters_by_site(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(make_switch(switch_id=2, name="icolo-sw", site="iColo NBO1"))
        body = (await auth_client.get(PATH, params={"site": "iColo NBO1"})).json()
        assert [item["name"] for item in body["data"]] == ["icolo-sw"]

    async def test_filters_by_active_flag(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(make_switch(switch_id=2, name="retired", is_active=False))
        body = (await auth_client.get(PATH, params={"is_active": "false"})).json()
        assert [item["name"] for item in body["data"]] == ["retired"]

    async def test_free_text_search(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(
            make_switch(switch_id=2, name="edge-sw", description="SIP provider trunk")
        )
        body = (await auth_client.get(PATH, params={"q": "sip provider"})).json()
        assert [item["name"] for item in body["data"]] == ["edge-sw"]

    async def test_pagination_window(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        for index in range(2, 8):
            switch_repository.seed(make_switch(switch_id=index, name=f"sw{index:02d}"))
        body = (await auth_client.get(PATH, params={"page": 2, "page_size": 3})).json()
        assert body["pagination"]["total"] == 7
        assert body["pagination"]["total_pages"] == 3
        assert body["pagination"]["has_next"] is True
        assert len(body["data"]) == 3

    async def test_page_size_above_the_ceiling_is_rejected(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(PATH, params={"page_size": 100_000})
        assert response.status_code == 422

    async def test_zero_page_is_rejected(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(PATH, params={"page": 0})).status_code == 422

    async def test_results_are_ordered_by_name(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(make_switch(switch_id=2, name="aaa-sw"))
        switch_repository.seed(make_switch(switch_id=3, name="zzz-sw"))
        names = [item["name"] for item in (await auth_client.get(PATH)).json()["data"]]
        assert names == sorted(names)


class TestGetSwitch:
    async def test_returns_the_switch_directly(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(f"{PATH}/1")
        assert response.status_code == 200
        body = response.json()
        # Single resources are returned bare, not wrapped in "data".
        assert body["id"] == 1
        assert "data" not in body

    async def test_unknown_id_returns_404_envelope(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(f"{PATH}/424242")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "SWITCH_NOT_FOUND"
        assert error["details"]["switch_id"] == 424242

    async def test_non_numeric_id_is_a_validation_error(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(f"{PATH}/abc")).status_code == 422

    async def test_zero_id_is_rejected(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(f"{PATH}/0")).status_code == 422

    async def test_credential_status_missing_is_surfaced(
        self, auth_client: AsyncClient, switch_repository: InMemorySwitchRepository
    ) -> None:
        switch_repository.seed(make_switch(switch_id=2, name="no-cred-sw", credential_ref="absent"))
        body = (await auth_client.get(f"{PATH}/2")).json()
        assert body["credential_status"] == "missing"


class TestOpenApi:
    async def test_document_is_served(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert f"{PATH}" in schema["paths"]
        assert "{switch_id}" in "".join(schema["paths"])

    async def test_api_key_security_scheme_is_advertised(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        schemes = schema["components"]["securitySchemes"]
        assert any(
            scheme.get("in") == "header" and scheme.get("name") == "X-API-Key"
            for scheme in schemes.values()
        )
