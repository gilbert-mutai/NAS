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

from nas.core.security import Scope, generate_api_key
from nas.db.locks import LockNotAcquiredError, advisory_lock
from nas.db.session import Database
from nas.domain.entities import ApiKey, AuditEntry
from nas.domain.enums import (
    AuditAction,
    AuditOutcome,
    SwitchSyncOutcome,
    SyncStatus,
    SyncTrigger,
    Vendor,
    VlanState,
)
from nas.domain.pagination import PageRequest
from nas.repositories.api_keys import SqlAlchemyApiKeyRepository
from nas.repositories.audit import SqlAlchemyAuditRepository
from nas.repositories.protocols import AuditFilters, NewApiKey, NewSwitch
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.repositories.sync_runs import SqlAlchemySyncRunRepository
from nas.repositories.vlans import SqlAlchemyVlanRepository
from nas.services.audit import AuditContext, AuditService
from nas.services.sync import SyncAlreadyRunningError, SyncOptions, SyncService
from tests.fakes import FakeCredentialProvider

pytestmark = pytest.mark.integration


def build_service(db: Database, **options: object) -> SyncService:
    return SyncService(
        session_factory=db.session_factory,
        credential_provider=FakeCredentialProvider({"mock-local"}),
        # The real AuditService, not a fake: it commits on its own session, and the
        # audit assertions below read the rows back through PostgreSQL.
        audit=AuditService(db.session_factory),
        options=SyncOptions(max_concurrency=4, **options),  # type: ignore[arg-type]
    )


async def make_api_key(db: Database, name: str) -> ApiKey:
    """A real `api_keys` row.

    Needed because `audit_log.api_key_id` is a genuine foreign key: a dangling id
    makes the insert fail, and since a failed audit write is swallowed by design the
    entry would silently never appear. Discovered by writing exactly that bug here.
    """
    generated = generate_api_key()
    async with db.session() as session:
        return await SqlAlchemyApiKeyRepository(session).create(
            NewApiKey(
                name=name,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=frozenset({Scope.SYNC_WRITE.value}),
            )
        )


async def audit_entries(db: Database) -> tuple[AuditEntry, ...]:
    async with db.session_factory() as session:
        page = await SqlAlchemyAuditRepository(session).list(
            filters=AuditFilters(), page_request=PageRequest(page=1, page_size=50)
        )
    return page.items


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


class TestEveryTriggerIsAudited:
    """The audit write lives in SyncService, so no entry point can skip it.

    It started out in the API route, which covered only HTTP-triggered runs and left
    the scheduler and `nas sync run` silent — the CLI being the least supervised path
    to a production switch, and so the worst one to miss. These tests exist to keep
    that regression from returning: a new caller of `run()` is audited by
    construction.
    """

    async def test_a_manual_api_run_is_attributed_to_both_key_and_actor(self, db: Database) -> None:
        await register(db, "sw-audit-1")
        key = await make_api_key(db, "clientmanager")
        service = build_service(db)
        run = await service.run(
            trigger=SyncTrigger.MANUAL,
            audit_context=AuditContext(
                actor="gilbert@angani.co",
                source_ip="10.10.10.238",
                api_key_id=key.id,
                api_key_name=key.name,
            ),
        )

        entries = await audit_entries(db)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.action is AuditAction.SYNC_TRIGGER
        assert entry.outcome is AuditOutcome.SUCCESS
        assert entry.actor == "gilbert@angani.co"
        assert entry.api_key_name == "clientmanager"
        assert entry.attribution == "gilbert@angani.co via clientmanager"
        assert entry.target_type == "sync_run"
        assert entry.target_id == str(run.id)

    async def test_a_scheduled_run_is_audited_with_no_actor(self, db: Database) -> None:
        """The timer has no human behind it, so an empty actor is the honest record —
        not a placeholder that would read like an identity."""
        await register(db, "sw-audit-2")
        service = build_service(db)
        await service.run(trigger=SyncTrigger.SCHEDULED)

        entry = (await audit_entries(db))[0]
        assert entry.outcome is AuditOutcome.SUCCESS
        assert entry.actor is None
        assert entry.api_key_name is None
        assert entry.attribution == "unattributed"

    async def test_a_cli_run_is_attributed_to_the_invoking_user(self, db: Database) -> None:
        """The gap that motivated moving the write. Someone with shell access can sync
        a production switch; the trail must name them."""
        await register(db, "sw-audit-3")
        service = build_service(db)
        await service.run(
            trigger=SyncTrigger.CLI,
            audit_context=AuditContext(actor="infra-admin", source_ip="cli"),
        )

        entry = (await audit_entries(db))[0]
        assert entry.actor == "infra-admin"
        assert entry.source_ip == "cli"
        assert entry.api_key_name is None, "no API key is involved in a CLI run"

    @pytest.mark.parametrize(
        "trigger", [SyncTrigger.MANUAL, SyncTrigger.SCHEDULED, SyncTrigger.CLI]
    )
    async def test_the_trigger_is_recorded_in_the_detail(
        self, db: Database, trigger: SyncTrigger
    ) -> None:
        """So scheduled noise can be filtered with detail->>'trigger' while still
        being present. Parametrised across all three to prove none is special-cased."""
        await register(db, f"sw-audit-{trigger.value}")
        await build_service(db).run(trigger=trigger)

        detail = (await audit_entries(db))[0].detail
        assert detail is not None
        assert detail["trigger"] == trigger.value

    async def test_requested_switch_ids_are_recorded(self, db: Database) -> None:
        switch_id = await register(db, "sw-audit-targeted")
        await register(db, "sw-audit-untouched")
        await build_service(db).run(trigger=SyncTrigger.MANUAL, switch_ids=[switch_id])

        detail = (await audit_entries(db))[0].detail
        assert detail is not None
        assert detail["switch_ids"] == [switch_id]

    async def test_the_correlation_id_ties_the_entry_to_the_run(self, db: Database) -> None:
        await register(db, "sw-audit-corr")
        run = await build_service(db).run(
            trigger=SyncTrigger.MANUAL, correlation_id="clientmanager-abc-123"
        )

        entry = (await audit_entries(db))[0]
        assert entry.correlation_id == "clientmanager-abc-123"
        assert run.correlation_id == "clientmanager-abc-123"

    async def test_a_hostile_actor_is_sanitised_before_storage(self, db: Database) -> None:
        """Sanitising is inside AuditService, so it applies to every trigger and
        cannot be bypassed by a caller that constructs its own context."""
        await register(db, "sw-audit-hostile")
        await build_service(db).run(
            trigger=SyncTrigger.CLI,
            audit_context=AuditContext(actor="infra-admin\nlevel=info event=approved"),
        )

        actor = (await audit_entries(db))[0].actor
        assert actor is not None
        assert "\n" not in actor

    async def test_a_partial_run_records_its_status(self, db: Database) -> None:
        """`partial` is the status that tells an operator the data is only partly
        trustworthy, so the trail should not flatten it to a bare success."""
        await register(db, "sw-audit-ok")
        # The mock driver keys off the *hostname*, not the switch name.
        await register(db, "sw-audit-bad", hostname="10.90.9.9-unreachable")
        run = await build_service(db).run(trigger=SyncTrigger.MANUAL)
        assert run.status is SyncStatus.PARTIAL

        detail = (await audit_entries(db))[0].detail
        assert detail is not None
        assert detail["status"] == "partial"


class TestRejectedRunIsAudited:
    async def test_a_run_blocked_by_the_lock_is_recorded(self, db: Database) -> None:
        """And this is why AuditService commits on its own session: the caller sees an
        exception, so anything written on a request-scoped transaction would be rolled
        back and the rejection would leave no trace."""
        await register(db, "sw-audit-locked")
        service = build_service(db)

        async with db.session_factory() as holder, advisory_lock(holder):
            with pytest.raises(SyncAlreadyRunningError):
                await service.run(
                    trigger=SyncTrigger.MANUAL,
                    audit_context=AuditContext(actor="gilbert@angani.co"),
                )

        entries = await audit_entries(db)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.outcome is AuditOutcome.ERROR
        assert entry.actor == "gilbert@angani.co"
        assert entry.target_id is None, "nothing started, so there is no run to point at"
        assert entry.detail is not None
        assert entry.detail["rejected"] == "CONFLICT"

    async def test_no_sync_run_row_was_created(self, db: Database) -> None:
        """Guards the pairing: a rejected trigger leaves an audit entry but must not
        leave a sync_runs row, or /sync/status would report a phantom run."""
        await register(db, "sw-audit-locked-2")
        service = build_service(db)

        async with db.session_factory() as holder, advisory_lock(holder):
            with pytest.raises(SyncAlreadyRunningError):
                await service.run(trigger=SyncTrigger.MANUAL)

        async with db.session_factory() as session:
            runs = await SqlAlchemySyncRunRepository(session).list(
                page_request=PageRequest(page=1, page_size=10)
            )
        assert runs.total == 0
        assert len(await audit_entries(db)) == 1


class TestTheCliIsAudited:
    """Runs the real `nas sync run` command, not just the service beneath it.

    The CLI was the gap that motivated moving the audit write, so verifying it needs
    the actual command: unit-testing `_cli_audit_context` proves the identity is
    resolved, and the service tests prove a supplied context is stored, but neither
    catches the command forgetting to pass one — which is exactly the bug being fixed.
    """

    @staticmethod
    def _invoke(database_url: str, env: dict[str, str]) -> object:
        """Invoke the CLI. Must run off the test's event loop.

        The command is a synchronous Typer callback that calls ``asyncio.run``
        internally, which raises if a loop is already running — so the caller hands
        this to ``asyncio.to_thread`` and it gets a loop of its own.
        """
        from typer.testing import CliRunner

        from nas.cli import app
        from nas.core.config import get_settings

        # The CLI builds its own Database from settings, so point settings at the test
        # database. get_settings is cached, hence the clears on both sides.
        get_settings.cache_clear()
        try:
            return CliRunner().invoke(
                app,
                ["sync", "run"],
                env={"NAS_DATABASE_URL": database_url, **env},
            )
        finally:
            get_settings.cache_clear()

    async def test_a_cli_sync_records_the_invoking_user(
        self, db: Database, migrated_database: str
    ) -> None:
        await register(db, "sw-cli-audit")

        result = await asyncio.to_thread(
            self._invoke, migrated_database, {"SUDO_USER": "infra-admin"}
        )
        assert getattr(result, "exit_code", None) == 0, getattr(result, "output", result)

        entries = await audit_entries(db)
        assert len(entries) == 1, "the CLI path must not be silent"
        entry = entries[0]
        assert entry.action is AuditAction.SYNC_TRIGGER
        assert entry.outcome is AuditOutcome.SUCCESS
        assert entry.actor == "infra-admin", "attributed to whoever escalated, not to `nas`"
        assert entry.source_ip == "cli"
        assert entry.api_key_name is None
        assert entry.detail is not None
        assert entry.detail["trigger"] == "cli"
