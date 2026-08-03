"""Switch inventory use-cases."""

from __future__ import annotations

import pytest

from nas.core.credentials import NullCredentialProvider
from nas.core.errors import SwitchNotFoundError
from nas.domain.enums import CredentialStatus, Vendor
from nas.domain.pagination import PageRequest
from nas.repositories.protocols import SwitchFilters
from nas.services.switches import SwitchService
from tests.fakes import FakeCredentialProvider, InMemorySwitchRepository, make_switch


def build_service(
    switches: list[object] | None = None,
    *,
    credential_refs: set[str] | None = None,
    null_provider: bool = False,
) -> SwitchService:
    repository = InMemorySwitchRepository(switches or [make_switch()])  # type: ignore[arg-type]
    provider = (
        NullCredentialProvider() if null_provider else FakeCredentialProvider(credential_refs)
    )
    return SwitchService(repository=repository, credential_provider=provider)


class TestGetSwitch:
    async def test_returns_the_switch(self) -> None:
        view = await build_service().get_switch(1)
        assert view.switch.name == "adc-core-sw1"

    async def test_unknown_id_raises_with_context(self) -> None:
        with pytest.raises(SwitchNotFoundError) as exc_info:
            await build_service().get_switch(999)
        assert exc_info.value.details["switch_id"] == 999


class TestCredentialStatus:
    async def test_resolved_when_reference_exists(self) -> None:
        view = await build_service(credential_refs={"juniper-core"}).get_switch(1)
        assert view.credential_status is CredentialStatus.RESOLVED

    async def test_missing_when_reference_absent(self) -> None:
        view = await build_service(credential_refs=set()).get_switch(1)
        assert view.credential_status is CredentialStatus.MISSING

    async def test_not_configured_when_no_store(self) -> None:
        view = await build_service(null_provider=True).get_switch(1)
        assert view.credential_status is CredentialStatus.NOT_CONFIGURED

    async def test_no_secret_leaks_into_the_view(self) -> None:
        view = await build_service().get_switch(1)
        rendered = repr(view)
        assert "unused-in-tests" not in rendered
        assert view.switch.credential_ref == "juniper-core"


class TestListSwitches:
    async def test_lists_all_by_default(self) -> None:
        switches = [make_switch(switch_id=i, name=f"sw{i}") for i in range(1, 4)]
        page = await build_service(switches).list_switches(
            filters=SwitchFilters(), page_request=PageRequest()
        )
        assert page.total == 3
        assert len(page.items) == 3

    async def test_filters_by_vendor(self) -> None:
        switches = [
            make_switch(switch_id=1, name="jun", vendor=Vendor.JUNIPER),
            make_switch(switch_id=2, name="cis", vendor=Vendor.CISCO),
        ]
        page = await build_service(switches).list_switches(
            filters=SwitchFilters(vendor=Vendor.CISCO), page_request=PageRequest()
        )
        assert [s.switch.name for s in page.items] == ["cis"]

    async def test_filters_by_active_flag(self) -> None:
        switches = [
            make_switch(switch_id=1, name="on", is_active=True),
            make_switch(switch_id=2, name="off", is_active=False),
        ]
        page = await build_service(switches).list_switches(
            filters=SwitchFilters(is_active=False), page_request=PageRequest()
        )
        assert [s.switch.name for s in page.items] == ["off"]

    async def test_search_matches_description(self) -> None:
        switches = [
            make_switch(switch_id=1, name="a", description="SIP trunk uplink"),
            make_switch(switch_id=2, name="b", description="management"),
        ]
        page = await build_service(switches).list_switches(
            filters=SwitchFilters(search="sip"), page_request=PageRequest()
        )
        assert [s.switch.name for s in page.items] == ["a"]

    async def test_pagination_totals_reflect_the_full_result_set(self) -> None:
        switches = [make_switch(switch_id=i, name=f"sw{i:02d}") for i in range(1, 8)]
        page = await build_service(switches).list_switches(
            filters=SwitchFilters(), page_request=PageRequest(page=2, page_size=3)
        )
        assert page.total == 7
        assert page.total_pages == 3
        assert len(page.items) == 3
        assert page.has_next is True
        assert page.has_previous is True

    async def test_credential_status_is_computed_per_item(self) -> None:
        switches = [
            make_switch(switch_id=1, name="a", credential_ref="juniper-core"),
            make_switch(switch_id=2, name="b", credential_ref="absent"),
        ]
        page = await build_service(switches, credential_refs={"juniper-core"}).list_switches(
            filters=SwitchFilters(), page_request=PageRequest()
        )
        statuses = {view.switch.name: view.credential_status for view in page.items}
        assert statuses == {
            "a": CredentialStatus.RESOLVED,
            "b": CredentialStatus.MISSING,
        }
