"""Domain entity and pagination behaviour."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from nas.domain.entities import ApiKey
from nas.domain.enums import CredentialStatus, ReachabilityState, Vendor
from nas.domain.pagination import MAX_PAGE_SIZE, Page, PageRequest
from tests.fakes import make_switch

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def build_api_key(**overrides: object) -> ApiKey:
    values: dict[str, object] = {
        "id": 1,
        "name": "crm",
        "prefix": "1a2b3c4d",
        "key_hash": "0" * 64,
        "scopes": frozenset({"switches:read", "vlans:read"}),
        "is_active": True,
        "created_at": NOW,
    }
    values.update(overrides)
    return ApiKey(**values)  # type: ignore[arg-type]


class TestApiKey:
    def test_active_key_without_expiry_is_usable(self) -> None:
        assert build_api_key().is_usable(now=NOW) is True

    def test_inactive_key_is_not_usable(self) -> None:
        assert build_api_key(is_active=False).is_usable(now=NOW) is False

    def test_future_expiry_is_usable(self) -> None:
        key = build_api_key(expires_at=NOW + timedelta(days=1))
        assert key.is_expired(now=NOW) is False
        assert key.is_usable(now=NOW) is True

    def test_past_expiry_is_not_usable(self) -> None:
        key = build_api_key(expires_at=NOW - timedelta(seconds=1))
        assert key.is_expired(now=NOW) is True
        assert key.is_usable(now=NOW) is False

    def test_expiry_boundary_is_exclusive(self) -> None:
        """A key expiring exactly now is already expired."""
        assert build_api_key(expires_at=NOW).is_expired(now=NOW) is True

    def test_scope_checks(self) -> None:
        key = build_api_key()
        assert key.has_scope("switches:read") is True
        assert key.has_scope("sync:write") is False
        assert key.has_all_scopes(frozenset({"switches:read", "vlans:read"})) is True
        assert key.has_all_scopes(frozenset({"switches:read", "sync:write"})) is False

    def test_empty_required_scopes_are_always_satisfied(self) -> None:
        assert build_api_key().has_all_scopes(frozenset()) is True

    def test_repr_excludes_key_hash(self) -> None:
        assert "0" * 64 not in repr(build_api_key())


class TestSwitch:
    def test_reachability_unknown_when_never_checked(self) -> None:
        """None is distinct from False: never checked is not the same as down."""
        assert make_switch().reachability is ReachabilityState.UNKNOWN

    def test_reachability_reachable(self) -> None:
        switch = replace(make_switch(), is_reachable=True)
        assert switch.reachability is ReachabilityState.REACHABLE

    def test_reachability_unreachable(self) -> None:
        switch = replace(make_switch(), is_reachable=False)
        assert switch.reachability is ReachabilityState.UNREACHABLE

    def test_credential_status_resolved(self) -> None:
        status = make_switch().credential_status(store_configured=True, resolvable=True)
        assert status is CredentialStatus.RESOLVED

    def test_credential_status_missing(self) -> None:
        status = make_switch().credential_status(store_configured=True, resolvable=False)
        assert status is CredentialStatus.MISSING

    def test_credential_status_not_configured_takes_precedence(self) -> None:
        status = make_switch().credential_status(store_configured=False, resolvable=False)
        assert status is CredentialStatus.NOT_CONFIGURED


class TestVendor:
    def test_juniper_and_mock_are_implemented(self) -> None:
        assert Vendor.JUNIPER.is_implemented is True
        assert Vendor.MOCK.is_implemented is True

    def test_other_vendors_are_declared_but_not_implemented(self) -> None:
        assert Vendor.CISCO.is_implemented is False
        assert Vendor.HUAWEI.is_implemented is False

    def test_every_vendor_has_a_label(self) -> None:
        assert all(vendor.label for vendor in Vendor)


class TestPageRequest:
    def test_defaults(self) -> None:
        request = PageRequest()
        assert request.page == 1
        assert request.offset == 0

    def test_offset_derives_from_page(self) -> None:
        assert PageRequest(page=3, page_size=20).offset == 40

    def test_page_is_floored_at_one(self) -> None:
        assert PageRequest(page=0).page == 1
        assert PageRequest(page=-5).page == 1

    def test_page_size_is_clamped_to_the_ceiling(self) -> None:
        assert PageRequest(page_size=10_000).page_size == MAX_PAGE_SIZE

    def test_page_size_is_floored_at_one(self) -> None:
        assert PageRequest(page_size=0).page_size == 1


class TestPage:
    def test_total_pages_rounds_up(self) -> None:
        assert Page(items=(), total=101, page=1, page_size=50).total_pages == 3

    def test_exact_division(self) -> None:
        assert Page(items=(), total=100, page=1, page_size=50).total_pages == 2

    def test_empty_result_set(self) -> None:
        page = Page(items=(), total=0, page=1, page_size=50)
        assert page.total_pages == 0
        assert page.has_next is False
        assert page.has_previous is False

    def test_navigation_flags_in_the_middle(self) -> None:
        page = Page(items=(), total=150, page=2, page_size=50)
        assert page.has_next is True
        assert page.has_previous is True

    def test_last_page_has_no_next(self) -> None:
        page = Page(items=(), total=150, page=3, page_size=50)
        assert page.has_next is False
        assert page.has_previous is True
