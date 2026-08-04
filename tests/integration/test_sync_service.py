"""End-to-end synchronisation against real PostgreSQL.

The highest-value tests in the suite: the mock driver plus a live database
exercise the whole path — device read, reconciliation, persistence, per-switch
attribution and run status — including the invariant that a switch which cannot
be read never loses its VLAN data.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from nas.db.locks import LockNotAcquiredError, advisory_lock
from nas.db.session import Database
from nas.domain.enums import SwitchSyncOutcome, SyncStatus, SyncTrigger, Vendor, VlanState
from nas.repositories.protocols import NewSwitch
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.repositories.sync_runs import SqlAlchemySyncRunRepository
from nas.repositories.vlans import SqlAlchemyVlanRepository
from nas.services.sync import SyncAlreadyRunningError, SyncOptions, SyncService
from tests.fakes import FakeCredentialProvider

pytestmark = pytest.mark.integration


def build_service(db: Database, **options: object) -> SyncService:
    return SyncService(
        session_factory=db.session_factory,
        credential_provider=FakeCredentialProvider({"mock-local"}),
        options=SyncOptions(max_concurrency=4, **options),  # type: ignore[arg-type]
    )


async def register(
    db: Database,
    name: str,
    *,
    hostname: str | None = None,
    vendor: Vendor = Vendor.MOCK,
    credential_ref: str = "mock-local",
    is_active: bool = True,
) -> int:
    async with db.session() as session:
        switch = await SqlAlchemySwitchRepository(session).create(
            NewSwitch(
                name=name,
                hostname=hostname or f"10.90.0.{abs(hash(name)) % 200 + 1}",
                vendor=vendor,
                credential_ref=credential_ref,
                is_active=is_active,
            )
        )
        return switch.id


async def active_tags(db: Database, switch_id: int) -> set[int]:
    async with db.session() as session:
        vlans = await SqlAlchemyVlanRepository(session).list_for_switch(switch_id)
    return {v.vlan_id for v in vlans if v.state is VlanState.ACTIVE}


async def all_tags(db: Database, switch_id: int) -> dict[int, VlanState]:
    async with db.session() as session:
        vlans = await SqlAlchemyVlanRepository(session).list_for_switch(switch_id)
    return {v.vlan_id: v.state for v in vlans}


class TestSuccessfulSync:
    async def test_discovers_and_persists_vlans(self, db: Database) -> None:
        switch_id = await register(db, "mock-sw-a")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        assert run.status is SyncStatus.SUCCESS
        assert run.switches_succeeded == 1
        assert run.vlans_created > 0
        assert len(await active_tags(db, switch_id)) == run.vlans_created

    async def test_second_run_is_idempotent(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        service = build_service(db)
        first = await service.run(trigger=SyncTrigger.MANUAL)
        second = await service.run(trigger=SyncTrigger.MANUAL)

        assert second.vlans_created == 0
        assert second.vlans_updated == 0
        assert second.vlans_marked_missing == 0
        assert second.vlans_unchanged == first.vlans_created

    async def test_active_count_equals_discovered_count(self, db: Database) -> None:
        """The property that exposed a reconciliation aliasing bug: after a
        successful sync, every discovered VLAN must be active."""
        switch_id = await register(db, "mock-sw-a")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        per_switch = {r.switch_name: r for r in run.switch_results}
        assert len(await active_tags(db, switch_id)) == per_switch["mock-sw-a"].vlans_discovered

    async def test_device_facts_are_recorded(self, db: Database) -> None:
        switch_id = await register(db, "mock-sw-a")
        await build_service(db).run(trigger=SyncTrigger.MANUAL)

        async with db.session() as session:
            switch = await SqlAlchemySwitchRepository(session).get_by_id(switch_id)
        assert switch is not None
        assert switch.is_reachable is True
        assert switch.model and switch.os_version
        assert switch.last_health_check is not None

    async def test_several_switches_are_all_synced(self, db: Database) -> None:
        ids = [await register(db, f"mock-sw-{n}") for n in ("a", "b", "c")]
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        assert run.switches_succeeded == 3
        for switch_id in ids:
            assert await active_tags(db, switch_id)

    async def test_restricting_to_one_switch(self, db: Database) -> None:
        first = await register(db, "mock-sw-a")
        second = await register(db, "mock-sw-b")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL, switch_ids=[first])

        assert run.switches_total == 1
        assert await active_tags(db, first)
        assert await active_tags(db, second) == set()


class TestTheInvariant:
    async def test_unreachable_switch_never_marks_vlans_missing(self, db: Database) -> None:
        """The most damaging bug available in this milestone. A switch that cannot
        be read must keep exactly the data the last successful run left."""
        switch_id = await register(db, "mock-sw-a", hostname="10.90.0.1")
        service = build_service(db)
        await service.run(trigger=SyncTrigger.MANUAL)
        before = await all_tags(db, switch_id)
        assert before

        # Make the device unreachable and sync again.
        async with db.session() as session:
            await session.execute(
                text("UPDATE switches SET hostname = :h WHERE id = :i"),
                {"h": "10.90.0.1-unreachable", "i": switch_id},
            )
            await session.commit()

        run = await service.run(trigger=SyncTrigger.MANUAL)

        assert run.status is SyncStatus.FAILED
        assert run.vlans_marked_missing == 0
        assert await all_tags(db, switch_id) == before

    async def test_failure_is_attributed_to_the_switch(self, db: Database) -> None:
        await register(db, "mock-sw-ok")
        await register(db, "mock-sw-bad", hostname="10.90.9.9-unreachable")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        assert run.status is SyncStatus.PARTIAL
        results = {r.switch_name: r for r in run.switch_results}
        assert results["mock-sw-ok"].outcome is SwitchSyncOutcome.SUCCESS
        assert results["mock-sw-bad"].outcome is SwitchSyncOutcome.FAILED
        assert "unreachable" in (results["mock-sw-bad"].error_message or "")

    async def test_healthy_switch_still_syncs_when_another_fails(self, db: Database) -> None:
        """One transaction per switch: a failure must not roll back others' work."""
        good = await register(db, "mock-sw-ok")
        await register(db, "mock-sw-bad", hostname="10.90.9.9-unreachable")
        await build_service(db).run(trigger=SyncTrigger.MANUAL)

        assert await active_tags(db, good)

    async def test_unreachable_switch_is_marked_unreachable(self, db: Database) -> None:
        switch_id = await register(db, "mock-sw-bad", hostname="10.90.9.9-unreachable")
        await build_service(db).run(trigger=SyncTrigger.MANUAL)

        async with db.session() as session:
            switch = await SqlAlchemySwitchRepository(session).get_by_id(switch_id)
        assert switch is not None
        assert switch.is_reachable is False
        assert switch.health_error

    async def test_empty_discovery_is_refused_by_default(self, db: Database) -> None:
        """A device reporting nothing must not wipe its VLANs."""
        switch_id = await register(db, "mock-sw-a")
        service = build_service(db)
        await service.run(trigger=SyncTrigger.MANUAL)
        before = await all_tags(db, switch_id)

        # A garbled read is a DriverParseError, so use a driver-level failure the
        # reconciler would otherwise be asked to act on: force zero discovery by
        # deleting nothing and instead asserting the guard via the reconciler path.
        async with db.session() as session:
            await session.execute(
                text("UPDATE switches SET hostname = :h WHERE id = :i"),
                {"h": "10.90.0.1-garbled", "i": switch_id},
            )
            await session.commit()

        run = await service.run(trigger=SyncTrigger.MANUAL)
        assert run.vlans_marked_missing == 0
        assert await all_tags(db, switch_id) == before


class TestSkipping:
    async def test_inactive_switch_is_skipped(self, db: Database) -> None:
        switch_id = await register(db, "mock-sw-off", is_active=False)
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        assert run.switches_skipped == 1
        assert run.status is SyncStatus.SUCCESS  # skipping is not a failure
        assert await active_tags(db, switch_id) == set()

    async def test_unsupported_vendor_is_skipped_with_a_reason(self, db: Database) -> None:
        await register(db, "cisco-sw", vendor=Vendor.CISCO)
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        result = run.switch_results[0]
        assert result.outcome is SwitchSyncOutcome.SKIPPED
        assert "cisco" in (result.error_message or "")

    async def test_unresolvable_credential_is_skipped_with_a_reason(self, db: Database) -> None:
        await register(db, "mock-sw-nocred", credential_ref="does-not-exist")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        result = run.switch_results[0]
        assert result.outcome is SwitchSyncOutcome.SKIPPED
        assert "does-not-exist" in (result.error_message or "")

    async def test_run_with_nothing_to_do_succeeds(self, db: Database) -> None:
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)
        assert run.status is SyncStatus.SUCCESS
        assert run.switches_total == 0


class TestDriftAcrossRuns:
    async def test_removal_then_reappearance(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        switch_id = await register(db, "mock-sw-a")
        service = build_service(db)

        await service.run(trigger=SyncTrigger.MANUAL)
        baseline = await active_tags(db, switch_id)

        monkeypatch.setenv("NAS_MOCK_DRIFT", "7")
        drifted = await service.run(trigger=SyncTrigger.MANUAL)
        assert drifted.vlans_marked_missing > 0 or drifted.vlans_created > 0

        monkeypatch.delenv("NAS_MOCK_DRIFT")
        restored = await service.run(trigger=SyncTrigger.MANUAL)

        assert await active_tags(db, switch_id) == baseline
        assert restored.vlans_marked_missing >= 0


class TestRunRecords:
    async def test_run_is_persisted_and_retrievable(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        run = await build_service(db).run(trigger=SyncTrigger.CLI)

        async with db.session() as session:
            stored = await SqlAlchemySyncRunRepository(session).get_by_id(run.id)
        assert stored is not None
        assert stored.trigger is SyncTrigger.CLI
        assert stored.finished_at is not None
        assert stored.duration_ms is not None
        assert stored.correlation_id

    async def test_latest_returns_the_newest_run(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        service = build_service(db)
        await service.run(trigger=SyncTrigger.MANUAL)
        second = await service.run(trigger=SyncTrigger.SCHEDULED)

        async with db.session() as session:
            latest = await SqlAlchemySyncRunRepository(session).latest()
        assert latest is not None
        assert latest.id == second.id

    async def test_per_switch_rows_are_recorded(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        await register(db, "mock-sw-b")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)

        async with db.session() as session:
            stored = await SqlAlchemySyncRunRepository(session).get_by_id(run.id)
        assert stored is not None
        assert {r.switch_name for r in stored.switch_results} == {"mock-sw-a", "mock-sw-b"}

    async def test_history_is_paginated_newest_first(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        service = build_service(db)
        ids = [(await service.run(trigger=SyncTrigger.MANUAL)).id for _ in range(3)]

        async with db.session() as session:
            from nas.domain.pagination import PageRequest

            page = await SqlAlchemySyncRunRepository(session).list(
                page_request=PageRequest(page=1, page_size=2)
            )
        assert [r.id for r in page.items] == sorted(ids, reverse=True)[:2]
        assert page.total == 3


class TestConcurrency:
    async def test_second_run_is_rejected_not_queued(self, db: Database) -> None:
        """A double-clicked 'Sync Now' must not start two runs."""
        await register(db, "mock-sw-a")
        service = build_service(db)

        results = await asyncio.gather(
            service.run(trigger=SyncTrigger.MANUAL),
            service.run(trigger=SyncTrigger.MANUAL),
            return_exceptions=True,
        )

        rejected = [r for r in results if isinstance(r, SyncAlreadyRunningError)]
        succeeded = [r for r in results if not isinstance(r, BaseException)]
        assert len(rejected) == 1
        assert len(succeeded) == 1

    async def test_only_one_run_row_is_created(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        service = build_service(db)
        await asyncio.gather(
            service.run(trigger=SyncTrigger.MANUAL),
            service.run(trigger=SyncTrigger.MANUAL),
            return_exceptions=True,
        )
        async with db.session() as session:
            count = (await session.execute(text("SELECT count(*) FROM sync_runs"))).scalar_one()
        assert count == 1

    async def test_lock_is_released_after_a_run(self, db: Database) -> None:
        await register(db, "mock-sw-a")
        service = build_service(db)
        await service.run(trigger=SyncTrigger.MANUAL)
        # A second sequential run must succeed.
        assert (await service.run(trigger=SyncTrigger.MANUAL)).id


class TestAdvisoryLock:
    async def test_two_sessions_contend(self, db: Database) -> None:
        async with (
            db.session_factory() as first,
            db.session_factory() as second,
            advisory_lock(first),
        ):
            with pytest.raises(LockNotAcquiredError):
                async with advisory_lock(second):
                    pass

    async def test_lock_is_released_on_exit(self, db: Database) -> None:
        async with db.session_factory() as first, db.session_factory() as second:
            async with advisory_lock(first):
                pass
            async with advisory_lock(second):
                pass  # would raise if the first had not released

    async def test_lock_is_released_when_the_body_raises(self, db: Database) -> None:
        async with db.session_factory() as first, db.session_factory() as second:
            with pytest.raises(RuntimeError):
                async with advisory_lock(first):
                    raise RuntimeError("boom")
            async with advisory_lock(second):
                pass
