"""Audit persistence against real PostgreSQL.

Covers what the fakes cannot: that the migration produces a working table, that
SQL-level filters mean the same thing as the in-memory ones, and — most
importantly — that an audit entry survives a request whose transaction rolls back.
That last property is the whole reason AuditService owns a session factory, and it
is invisible to any test that does not use a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.security import Scope, generate_api_key
from nas.db.session import Database
from nas.domain.entities import MAX_ACTOR_LENGTH, ApiKey, AuditEntry
from nas.domain.enums import AuditAction, AuditOutcome
from nas.domain.pagination import PageRequest
from nas.repositories.api_keys import SqlAlchemyApiKeyRepository
from nas.repositories.audit import SqlAlchemyAuditRepository
from nas.repositories.protocols import AuditFilters, NewApiKey
from nas.services.audit import AuditService

pytestmark = pytest.mark.integration

T0 = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
ACTOR = "gilbert@angani.co"
PAGE = PageRequest(page=1, page_size=50)


def entry(
    *,
    action: AuditAction = AuditAction.SYNC_TRIGGER,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor: str | None = ACTOR,
    occurred_at: datetime = T0,
    api_key_id: int | None = None,
    api_key_name: str | None = "clientmanager",
    detail: dict[str, object] | None = None,
    target_id: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        action=action,
        outcome=outcome,
        occurred_at=occurred_at,
        api_key_id=api_key_id,
        api_key_name=api_key_name,
        actor=actor,
        source_ip="10.10.10.238",
        correlation_id="corr-1",
        target_type="sync_run" if target_id else None,
        target_id=target_id,
        detail=detail,
    )


async def make_key(session: AsyncSession, name: str = "clientmanager") -> ApiKey:
    generated = generate_api_key()
    return await SqlAlchemyApiKeyRepository(session).create(
        NewApiKey(
            name=name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=frozenset({Scope.SYNC_WRITE.value}),
        )
    )


class TestRoundTrip:
    async def test_an_entry_is_stored_and_read_back(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        stored = await repository.record(entry(target_id="42"))
        assert stored.id is not None

        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        assert page.total == 1
        found = page.items[0]
        assert found.id == stored.id
        assert found.action is AuditAction.SYNC_TRIGGER
        assert found.outcome is AuditOutcome.SUCCESS
        assert found.actor == ACTOR
        assert found.source_ip == "10.10.10.238"
        assert found.target_type == "sync_run"
        assert found.target_id == "42"

    async def test_jsonb_detail_round_trips(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        detail: dict[str, object] = {
            "switch_ids": [1, 2],
            "status": "partial",
            "nested": {"ok": True},
        }
        await repository.record(entry(detail=detail))
        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        assert page.items[0].detail == detail

    async def test_a_maximum_length_actor_fits_the_column(self, session: AsyncSession) -> None:
        """The column is 320 because that is the longest legal email address. A
        shorter column would raise on a legitimate identity and lose the entry."""
        actor = ("a" * 64) + "@" + ("b" * 251) + ".com"
        assert len(actor) == MAX_ACTOR_LENGTH
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry(actor=actor))
        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        assert page.items[0].actor == actor

    async def test_an_unattributed_entry_is_allowed(self, session: AsyncSession) -> None:
        """Every attribution column is nullable on purpose: an event with no known
        actor must still be recordable, rather than being dropped."""
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry(actor=None, api_key_name=None))
        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        assert page.items[0].attribution == "unattributed"


class TestSurvivingAKeyDeletion:
    async def test_deleting_the_key_nulls_the_fk_but_keeps_the_name(
        self, session: AsyncSession
    ) -> None:
        """Revoking and deleting a key must not erase the history of what it did.
        Hence ON DELETE SET NULL plus a name snapshot, not a plain foreign key.
        """
        key = await make_key(session)
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry(api_key_id=key.id, api_key_name=key.name))
        await session.commit()

        await session.execute(text("DELETE FROM api_keys WHERE id = :id"), {"id": key.id})
        await session.commit()

        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        assert page.total == 1, "the audit row must outlive the key"
        found = page.items[0]
        assert found.api_key_id is None
        assert found.api_key_name == "clientmanager"


class TestFilters:
    async def test_by_action(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry())
        await repository.record(entry(action=AuditAction.AUTH_DENIED, outcome=AuditOutcome.DENIED))
        page = await repository.list(
            filters=AuditFilters(action=AuditAction.AUTH_DENIED), page_request=PAGE
        )
        assert page.total == 1
        assert page.items[0].action is AuditAction.AUTH_DENIED

    async def test_by_outcome(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry())
        await repository.record(entry(outcome=AuditOutcome.ERROR))
        page = await repository.list(
            filters=AuditFilters(outcome=AuditOutcome.ERROR), page_request=PAGE
        )
        assert page.total == 1

    async def test_by_actor_is_case_insensitive_in_sql_too(self, session: AsyncSession) -> None:
        """The in-memory fake lowercases in Python; this proves the SQL agrees, so a
        test written against the fake means the same thing in production."""
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry(actor="Gilbert@Angani.co"))
        await repository.record(entry(actor="someone.else@angani.co"))
        page = await repository.list(filters=AuditFilters(actor=ACTOR), page_request=PAGE)
        assert page.total == 1
        assert page.items[0].actor == "Gilbert@Angani.co", "storage preserves the original case"

    async def test_by_since_is_inclusive(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry(occurred_at=T0 - timedelta(hours=1)))
        await repository.record(entry(occurred_at=T0))
        page = await repository.list(filters=AuditFilters(since=T0), page_request=PAGE)
        assert page.total == 1

    async def test_filters_combine(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        await repository.record(entry())
        await repository.record(entry(outcome=AuditOutcome.ERROR))
        await repository.record(entry(outcome=AuditOutcome.ERROR, actor="other@angani.co"))
        page = await repository.list(
            filters=AuditFilters(outcome=AuditOutcome.ERROR, actor=ACTOR), page_request=PAGE
        )
        assert page.total == 1

    async def test_the_count_respects_the_filters(self, session: AsyncSession) -> None:
        """A total that ignored the filters would make pagination lie."""
        repository = SqlAlchemyAuditRepository(session)
        for _ in range(5):
            await repository.record(entry())
        await repository.record(entry(outcome=AuditOutcome.ERROR))
        page = await repository.list(
            filters=AuditFilters(outcome=AuditOutcome.ERROR), page_request=PAGE
        )
        assert page.total == 1


class TestOrdering:
    async def test_newest_first(self, session: AsyncSession) -> None:
        repository = SqlAlchemyAuditRepository(session)
        for hours in range(3):
            await repository.record(entry(occurred_at=T0 + timedelta(hours=hours)))
        page = await repository.list(filters=AuditFilters(), page_request=PAGE)
        timestamps = [item.occurred_at for item in page.items]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_identical_timestamps_order_by_id_descending(self, session: AsyncSession) -> None:
        """Entries can share a timestamp — a scripted burst does exactly that.

        Documents the ordering contract: equal timestamps come back newest-id-first,
        so paging through a burst is stable.

        **This test does not prove the `id DESC` tiebreak is needed.** Removing it
        from the query leaves the test green: with a table this small PostgreSQL
        walks the `occurred_at` index backwards and returns equal keys in reverse
        insertion order regardless. The tiebreak stays because SQL does not *promise*
        that — a different plan, a parallel scan or more data could reorder equal
        keys, and then a paginated read could show a row twice or skip it. Verified
        by mutation, so nobody mistakes this for a regression test that would catch
        the tiebreak's removal.
        """
        repository = SqlAlchemyAuditRepository(session)
        stored = [await repository.record(entry(occurred_at=T0)) for _ in range(6)]
        newest_first = [item.id for item in reversed(stored)]

        seen: list[int] = []
        for page_number in (1, 2, 3):
            page = await repository.list(
                filters=AuditFilters(),
                page_request=PageRequest(page=page_number, page_size=2),
            )
            seen.extend(item.id for item in page.items if item.id is not None)

        assert seen == newest_first


class TestServiceCommitsIndependently:
    """The property that makes the rejected-sync entry possible.

    An audit entry written on the request's session would be rolled back with it —
    and a rejected `POST /sync` is precisely a request that raises. So AuditService
    takes a session *factory* and commits on its own session.
    """

    async def test_the_entry_survives_a_rolled_back_caller_session(
        self, db: Database, session: AsyncSession
    ) -> None:
        # Something the caller did, which is about to be thrown away.
        await session.execute(text("SELECT 1"))

        service = AuditService(db.session_factory)
        recorded = await service.record(
            action=AuditAction.SYNC_TRIGGER,
            outcome=AuditOutcome.ERROR,
            actor=ACTOR,
            api_key_name="clientmanager",
            detail={"rejected": "CONFLICT"},
        )
        assert recorded is not None

        # The request fails; its transaction is discarded.
        await session.rollback()

        async with db.session_factory() as verify:
            page = await SqlAlchemyAuditRepository(verify).list(
                filters=AuditFilters(), page_request=PAGE
            )
        assert page.total == 1, "the audit entry was rolled back with the request"
        assert page.items[0].outcome is AuditOutcome.ERROR

    async def test_the_actor_is_sanitised_on_the_way_in(self, db: Database) -> None:
        """Sanitising is inside the service, so persistence cannot be reached with an
        uncleaned actor even by a caller that skips the API layer."""
        service = AuditService(db.session_factory)
        await service.record(
            action=AuditAction.SYNC_TRIGGER,
            outcome=AuditOutcome.SUCCESS,
            actor="gilbert@angani.co\nX-Injected: yes",
        )
        async with db.session_factory() as verify:
            page = await SqlAlchemyAuditRepository(verify).list(
                filters=AuditFilters(), page_request=PAGE
            )
        actor = page.items[0].actor
        assert actor is not None
        assert "\n" not in actor

    async def test_a_write_failure_does_not_raise(self, db: Database) -> None:
        """By the time an entry is written the switches have been polled. Raising
        here would report failure for work that succeeded, and the caller's retry
        would poll them again. The failure goes to the log instead.
        """
        service = AuditService(db.session_factory)
        # A NUL byte is rejected by PostgreSQL outright, and nothing sanitises
        # target_id — so this reaches the driver and fails there.
        result = await service.record(
            action=AuditAction.SYNC_TRIGGER,
            outcome=AuditOutcome.SUCCESS,
            target_id="bad\x00id",
        )
        assert result is None, "a failed write returns None rather than raising"

        async with db.session_factory() as verify:
            page = await SqlAlchemyAuditRepository(verify).list(
                filters=AuditFilters(), page_request=PAGE
            )
        assert page.total == 0
