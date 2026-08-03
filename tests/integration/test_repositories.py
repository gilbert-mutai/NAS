"""Repository behaviour against real PostgreSQL.

Covers what in-memory fakes cannot: the migration produces a working schema, SQL
filters and ordering behave as intended, and database constraints reject bad data.

Skipped unless NAS_TEST_DATABASE_URL is set. See tests/integration/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.errors import ConflictError
from nas.core.security import Scope, generate_api_key
from nas.db.models import SwitchRow
from nas.domain.enums import Vendor
from nas.domain.pagination import PageRequest
from nas.repositories.api_keys import SqlAlchemyApiKeyRepository
from nas.repositories.protocols import NewApiKey, NewSwitch, SwitchFilters
from nas.repositories.switches import SqlAlchemySwitchRepository

pytestmark = pytest.mark.integration


def new_switch(**overrides: object) -> NewSwitch:
    values: dict[str, object] = {
        "name": "adc-core-sw1",
        "hostname": "10.20.0.11",
        "vendor": Vendor.JUNIPER,
        "credential_ref": "juniper-core",
        "site": "ADC NBO",
        "environment": "production",
    }
    values.update(overrides)
    return NewSwitch(**values)  # type: ignore[arg-type]


class TestSwitchRepository:
    async def test_create_and_read_back(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        created = await repository.create(new_switch())

        fetched = await repository.get_by_id(created.id)
        assert fetched is not None
        assert fetched.name == "adc-core-sw1"
        assert fetched.vendor is Vendor.JUNIPER
        assert fetched.port == 22
        assert fetched.is_active is True
        # Timestamps are database-generated and timezone-aware.
        assert fetched.created_at.tzinfo is not None

    async def test_unknown_id_returns_none(self, session: AsyncSession) -> None:
        assert await SqlAlchemySwitchRepository(session).get_by_id(999_999) is None

    async def test_get_by_name_is_case_insensitive(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(name="ADC-Core-SW1"))
        assert await repository.get_by_name("adc-core-sw1") is not None

    async def test_duplicate_name_raises_conflict(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch())
        with pytest.raises(ConflictError):
            await repository.create(new_switch(hostname="10.20.0.12"))

    async def test_names_are_trimmed_on_write(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        created = await repository.create(new_switch(name="  padded-sw  "))
        assert created.name == "padded-sw"

    async def test_blank_name_is_rejected_by_the_database(self, session: AsyncSession) -> None:
        """Defence in depth: the check constraint holds even for direct writes."""
        session.add(
            SwitchRow(
                name="   ",
                hostname="10.0.0.1",
                vendor=Vendor.JUNIPER.value,
                credential_ref="ref",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_out_of_range_port_is_rejected_by_the_database(
        self, session: AsyncSession
    ) -> None:
        session.add(
            SwitchRow(
                name="bad-port-sw",
                hostname="10.0.0.1",
                port=70_000,
                vendor=Vendor.JUNIPER.value,
                credential_ref="ref",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.flush()

    async def test_filter_by_vendor(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(name="jun-sw", vendor=Vendor.JUNIPER))
        await repository.create(new_switch(name="cis-sw", vendor=Vendor.CISCO))

        page = await repository.list(
            filters=SwitchFilters(vendor=Vendor.CISCO), page_request=PageRequest()
        )
        assert [s.name for s in page.items] == ["cis-sw"]

    async def test_filter_by_site_is_case_insensitive(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(site="iColo NBO1"))
        page = await repository.list(
            filters=SwitchFilters(site="icolo nbo1"), page_request=PageRequest()
        )
        assert page.total == 1

    async def test_search_spans_name_hostname_and_description(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(name="sw-a", description="SIP provider trunk"))
        await repository.create(new_switch(name="sw-b", hostname="10.99.0.1"))

        by_description = await repository.list(
            filters=SwitchFilters(search="sip provider"), page_request=PageRequest()
        )
        assert [s.name for s in by_description.items] == ["sw-a"]

        by_hostname = await repository.list(
            filters=SwitchFilters(search="10.99"), page_request=PageRequest()
        )
        assert [s.name for s in by_hostname.items] == ["sw-b"]

    async def test_search_wildcards_are_escaped(self, session: AsyncSession) -> None:
        """A '%' in the search term must match literally, not match everything."""
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(name="sw-plain"))
        await repository.create(new_switch(name="sw-100%-load"))

        page = await repository.list(
            filters=SwitchFilters(search="100%"), page_request=PageRequest()
        )
        assert [s.name for s in page.items] == ["sw-100%-load"]

    async def test_underscore_wildcard_is_escaped(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        await repository.create(new_switch(name="sw-ab"))
        await repository.create(new_switch(name="sw-a_b"))

        page = await repository.list(
            filters=SwitchFilters(search="a_b"), page_request=PageRequest()
        )
        assert [s.name for s in page.items] == ["sw-a_b"]

    async def test_pagination_totals_ignore_the_window(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        for index in range(7):
            await repository.create(new_switch(name=f"sw-{index:02d}"))

        page = await repository.list(
            filters=SwitchFilters(), page_request=PageRequest(page=2, page_size=3)
        )
        assert page.total == 7
        assert len(page.items) == 3
        assert [s.name for s in page.items] == ["sw-03", "sw-04", "sw-05"]

    async def test_results_are_ordered_by_name(self, session: AsyncSession) -> None:
        repository = SqlAlchemySwitchRepository(session)
        for name in ("sw-zebra", "sw-alpha", "sw-middle"):
            await repository.create(new_switch(name=name))
        page = await repository.list(filters=SwitchFilters(), page_request=PageRequest())
        assert [s.name for s in page.items] == ["sw-alpha", "sw-middle", "sw-zebra"]


class TestApiKeyRepository:
    async def test_create_and_look_up_by_prefix(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        generated = generate_api_key()
        created = await repository.create(
            NewApiKey(
                name="crm",
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SWITCHES_READ.value, Scope.VLANS_READ.value}),
            )
        )
        assert created.scopes == frozenset({Scope.SWITCHES_READ.value, Scope.VLANS_READ.value})

        fetched = await repository.get_by_prefix(generated.prefix)
        assert fetched is not None
        assert fetched.key_hash == generated.key_hash
        assert fetched.is_active is True

    async def test_plaintext_key_is_never_persisted(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        generated = generate_api_key()
        await repository.create(
            NewApiKey(
                name="crm",
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SWITCHES_READ.value}),
            )
        )
        stored = await repository.get_by_prefix(generated.prefix)
        assert stored is not None
        assert generated.plaintext not in stored.key_hash
        assert len(stored.key_hash) == 64

    async def test_unknown_prefix_returns_none(self, session: AsyncSession) -> None:
        assert await SqlAlchemyApiKeyRepository(session).get_by_prefix("deadbeef") is None

    async def test_duplicate_name_raises_conflict(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        for _ in range(1):
            generated = generate_api_key()
            await repository.create(
                NewApiKey(
                    name="crm",
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    scopes=frozenset({Scope.SWITCHES_READ.value}),
                )
            )
        other = generate_api_key()
        with pytest.raises(ConflictError):
            await repository.create(
                NewApiKey(
                    name="crm",
                    prefix=other.prefix,
                    key_hash=other.key_hash,
                    scopes=frozenset({Scope.SWITCHES_READ.value}),
                )
            )

    async def test_mark_used_records_the_timestamp(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        generated = generate_api_key()
        created = await repository.create(
            NewApiKey(
                name="crm",
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SWITCHES_READ.value}),
            )
        )
        assert created.last_used_at is None

        when = datetime.now(UTC)
        await repository.mark_used(created.id, when=when)
        session.expire_all()

        refreshed = await repository.get_by_prefix(generated.prefix)
        assert refreshed is not None
        assert refreshed.last_used_at is not None
        assert abs(refreshed.last_used_at - when) < timedelta(seconds=5)

    async def test_revoke_deactivates_the_key(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        generated = generate_api_key()
        await repository.create(
            NewApiKey(
                name="crm",
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SWITCHES_READ.value}),
            )
        )
        assert await repository.revoke("crm") is True
        session.expire_all()

        revoked = await repository.get_by_prefix(generated.prefix)
        assert revoked is not None
        assert revoked.is_active is False
        assert revoked.is_usable() is False

    async def test_revoking_twice_reports_no_change(self, session: AsyncSession) -> None:
        repository = SqlAlchemyApiKeyRepository(session)
        generated = generate_api_key()
        await repository.create(
            NewApiKey(
                name="crm",
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SWITCHES_READ.value}),
            )
        )
        assert await repository.revoke("crm") is True
        assert await repository.revoke("crm") is False

    async def test_revoking_an_unknown_name_reports_no_change(self, session: AsyncSession) -> None:
        assert await SqlAlchemyApiKeyRepository(session).revoke("nope") is False
