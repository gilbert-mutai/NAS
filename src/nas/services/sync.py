"""Synchronisation orchestration.

Polls switches, reconciles what they report against the database, and records a
run with per-switch attribution.

**The safety invariant of this milestone:** a switch that cannot be read never has
its VLANs marked missing. It is enforced structurally — the reconciler is only
reached inside the ``try`` after ``get_vlans`` has returned successfully. Any
DriverError skips straight to recording a failure, leaving stored VLAN data
untouched. Getting this wrong would look exactly like mass VLAN deletion.

Transaction shape: **one transaction per switch**, not one per run. A run that
fails on switch four keeps the work done for switches one to three. Combined with
per-switch outcome rows, a partial run is both durable and explainable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nas.core.credentials import CredentialError, CredentialProvider
from nas.core.errors import ConflictError
from nas.core.logging import get_logger
from nas.db.locks import LockNotAcquiredError, advisory_lock
from nas.domain.entities import Switch, SyncRun
from nas.domain.enums import AuditAction, AuditOutcome, SwitchSyncOutcome, SyncTrigger
from nas.domain.pagination import PageRequest
from nas.drivers.base import DriverError
from nas.drivers.options import DriverOptions
from nas.drivers.registry import open_driver, unsupported_reason
from nas.repositories.protocols import (
    SwitchFilters,
    SwitchSyncResult,
    SyncRunTotals,
)
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.repositories.sync_runs import SqlAlchemySyncRunRepository
from nas.repositories.vlans import SqlAlchemyVlanRepository
from nas.services.audit import AuditContext, AuditService
from nas.sync.reconciler import ReconciliationRefusedError, build_plan

logger = get_logger(__name__)

# Upper bound on switches loaded for one run. Far above any realistic inventory;
# exists so a runaway query cannot be unbounded.
_MAX_SWITCHES_PER_RUN = 500


class SyncAlreadyRunningError(ConflictError):
    """Another synchronisation holds the advisory lock."""

    message = "A synchronisation is already in progress."


@dataclass(frozen=True, slots=True)
class SyncOptions:
    max_concurrency: int = 4
    allow_empty_discovery: bool = False
    connect_timeout: int = 30
    command_timeout: int = 60
    stale_run_minutes: int = 60
    verify_device_tls: bool = False

    @property
    def driver_options(self) -> DriverOptions:
        return DriverOptions(
            connect_timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            verify_tls=self.verify_device_tls,
        )


class SyncService:
    """Runs a synchronisation pass.

    Takes a session *factory* rather than a session: it holds the advisory lock on
    one connection for the whole run while each switch commits independently on
    its own, and it must be callable from the scheduler and CLI where no HTTP
    request-scoped session exists.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        credential_provider: CredentialProvider,
        audit: AuditService,
        options: SyncOptions | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._credentials = credential_provider
        self._audit = audit
        self._options = options or SyncOptions()

    async def run(
        self,
        *,
        trigger: SyncTrigger,
        correlation_id: str | None = None,
        switch_ids: list[int] | None = None,
        audit_context: AuditContext | None = None,
    ) -> SyncRun:
        """Execute one synchronisation pass, and record it in the audit trail.

        Raises SyncAlreadyRunningError if another run holds the lock.

        **The audit write lives here, not in the API route.** Auditing at the route
        covered only HTTP-triggered runs, leaving the scheduler and — more
        importantly — `nas sync run` on the box unattributed. The CLI is the least
        supervised path to a production switch, so it was the worst one to miss. This
        method is the single choke point every trigger passes through, so recording
        here cannot be bypassed by adding another caller.

        ``audit_context`` carries what the service cannot know: the API passes the
        authenticated key plus the forwarded user and client address, the CLI passes
        the invoking OS user, and the scheduler passes nothing — an empty context is
        the honest record of machine-initiated work.
        """
        correlation_id = correlation_id or uuid.uuid4().hex
        log = logger.bind(correlation_id=correlation_id, trigger=trigger.value)
        attribution = audit_context or AuditContext()

        async def audit(
            outcome: AuditOutcome,
            *,
            target_id: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> None:
            await self._audit.record(
                action=AuditAction.SYNC_TRIGGER,
                outcome=outcome,
                api_key_id=attribution.api_key_id,
                api_key_name=attribution.api_key_name,
                actor=attribution.actor,
                source_ip=attribution.source_ip,
                correlation_id=correlation_id,
                target_type="sync_run" if target_id else None,
                target_id=target_id,
                # `trigger` is in the detail so scheduled noise can be filtered out
                # with detail->>'trigger', while still being present.
                detail={"trigger": trigger.value, "switch_ids": switch_ids} | (extra or {}),
            )

        # The lock lives on its own connection for the whole run. PostgreSQL frees
        # it automatically if this process dies, so a crash cannot wedge syncing.
        async with self._session_factory() as lock_session:
            try:
                async with advisory_lock(lock_session):
                    run = await self._run_locked(
                        trigger=trigger, correlation_id=correlation_id, switch_ids=switch_ids
                    )
            except LockNotAcquiredError as exc:
                log.info("sync_rejected_already_running")
                # Not a failure of the caller's: they were entitled to ask, and a run
                # was already under way. Recorded so a burst of rejected triggers is
                # visible rather than only appearing as 409s in the access log.
                await audit(AuditOutcome.ERROR, extra={"rejected": "CONFLICT"})
                raise SyncAlreadyRunningError from exc
            except Exception as exc:
                # "A sync was started and never finished" is what an operator needs
                # to see, so a crash must not leave the trail silent.
                await audit(AuditOutcome.ERROR, extra={"error": type(exc).__name__})
                raise

        await audit(
            AuditOutcome.SUCCESS,
            target_id=str(run.id),
            extra={"status": run.status.value, "switches_total": run.switches_total},
        )
        return run

    async def _run_locked(
        self, *, trigger: SyncTrigger, correlation_id: str, switch_ids: list[int] | None
    ) -> SyncRun:
        log = logger.bind(correlation_id=correlation_id, trigger=trigger.value)
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()

        # Any run still 'running' from a killed process would otherwise make
        # /sync/status report a sync in progress forever.
        await self._fail_stale_runs(now=started_at)

        switches = await self._load_switches(switch_ids)

        async with self._session_factory() as session:
            run = await SqlAlchemySyncRunRepository(session).start(
                trigger=trigger, correlation_id=correlation_id, started_at=started_at
            )

        log = log.bind(sync_run_id=run.id)
        log.info("sync_started", switch_count=len(switches))

        semaphore = asyncio.Semaphore(self._options.max_concurrency)

        async def guarded(switch: Switch) -> SwitchSyncResult:
            async with semaphore:
                return await self._sync_switch(switch, sync_run_id=run.id)

        results = await asyncio.gather(
            *(guarded(switch) for switch in switches), return_exceptions=False
        )

        totals = _totals(results)
        status = SyncRun.derive_status(
            succeeded=totals.switches_succeeded,
            failed=totals.switches_failed,
            skipped=totals.switches_skipped,
            attempted=totals.switches_succeeded + totals.switches_failed,
        )
        finished_at = datetime.now(UTC)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)

        async with self._session_factory() as session:
            finished = await SqlAlchemySyncRunRepository(session).finish(
                run.id,
                status=status,
                finished_at=finished_at,
                duration_ms=duration_ms,
                totals=totals,
                error_message=_summarise_failures(results),
            )
            await session.commit()

        log.info(
            "sync_finished",
            status=status.value,
            duration_ms=duration_ms,
            switches_succeeded=totals.switches_succeeded,
            switches_failed=totals.switches_failed,
            switches_skipped=totals.switches_skipped,
            vlans_created=totals.vlans_created,
            vlans_updated=totals.vlans_updated,
            vlans_marked_missing=totals.vlans_marked_missing,
        )
        return finished

    # ── One switch ────────────────────────────────────────────────────────────
    async def _sync_switch(self, switch: Switch, *, sync_run_id: int) -> SwitchSyncResult:
        switch_log = logger.bind(switch=switch.name, sync_run_id=sync_run_id)
        started = time.perf_counter()

        skip_reason = self._skip_reason(switch)
        if skip_reason is not None:
            switch_log.info("switch_skipped", reason=skip_reason)
            result = SwitchSyncResult(
                switch_id=switch.id,
                switch_name=switch.name,
                outcome=SwitchSyncOutcome.SKIPPED,
                duration_ms=_elapsed_ms(started),
                error_message=skip_reason,
            )
            await self._persist_switch_result(result, sync_run_id=sync_run_id)
            return result

        try:
            credential = self._credentials.get(switch.credential_ref)
        except CredentialError as exc:
            return await self._record_failure(
                switch, sync_run_id=sync_run_id, started=started, error=str(exc), reachable=None
            )

        try:
            async with open_driver(switch, credential, self._options.driver_options) as driver:
                facts = await driver.get_facts()
                discovered = await driver.get_vlans()
        except DriverError as exc:
            # ── The invariant ──
            # The device could not be read. Reconciliation is NOT attempted, so no
            # VLAN is created, updated or marked missing. Stored data is left
            # exactly as the last successful run left it.
            switch_log.warning("switch_unreadable", error=str(exc), error_type=type(exc).__name__)
            return await self._record_failure(
                switch, sync_run_id=sync_run_id, started=started, error=str(exc), reachable=False
            )

        try:
            async with self._session_factory() as session:
                vlans = SqlAlchemyVlanRepository(session)
                existing = await vlans.list_for_switch(switch.id)

                plan = build_plan(
                    switch_id=switch.id,
                    existing=existing,
                    discovered=discovered,
                    allow_empty_discovery=self._options.allow_empty_discovery,
                )

                observed_at = datetime.now(UTC)
                await vlans.apply_plan(plan, observed_at=observed_at)

                await SqlAlchemySwitchRepository(session).record_observation(
                    switch.id,
                    is_reachable=True,
                    checked_at=observed_at,
                    health_error=None,
                    model=facts.model,
                    os_version=facts.os_version,
                )

                counts = plan.counts
                result = SwitchSyncResult(
                    switch_id=switch.id,
                    switch_name=switch.name,
                    outcome=SwitchSyncOutcome.SUCCESS,
                    vlans_discovered=plan.discovered_count,
                    vlans_created=counts["created"],
                    vlans_updated=counts["updated"],
                    vlans_unchanged=counts["unchanged"],
                    vlans_marked_missing=counts["marked_missing"],
                    duration_ms=_elapsed_ms(started),
                )
                await SqlAlchemySyncRunRepository(session).record_switch(sync_run_id, result)
                await session.commit()

                switch_log.info("switch_synced", **counts, discovered=plan.discovered_count)
                return result
        except ReconciliationRefusedError as exc:
            # The device answered, but applying its answer would have been
            # destructive. Data untouched; the switch is reported as failed so the
            # run is partial and an operator investigates.
            switch_log.error("reconciliation_refused", error=str(exc))
            return await self._record_failure(
                switch, sync_run_id=sync_run_id, started=started, error=str(exc), reachable=True
            )

    def _skip_reason(self, switch: Switch) -> str | None:
        if not switch.is_active:
            return "Switch is marked inactive."
        unsupported = unsupported_reason(switch.vendor)
        if unsupported is not None:
            return unsupported
        if not self._credentials.has(switch.credential_ref):
            return (
                f"Credential {switch.credential_ref!r} does not resolve. "
                "Add it to the credential store."
            )
        return None

    async def _record_failure(
        self,
        switch: Switch,
        *,
        sync_run_id: int,
        started: float,
        error: str,
        reachable: bool | None,
    ) -> SwitchSyncResult:
        result = SwitchSyncResult(
            switch_id=switch.id,
            switch_name=switch.name,
            outcome=SwitchSyncOutcome.FAILED,
            duration_ms=_elapsed_ms(started),
            error_message=error[:2000],
        )
        await self._persist_switch_result(result, sync_run_id=sync_run_id, reachable=reachable)
        return result

    async def _persist_switch_result(
        self,
        result: SwitchSyncResult,
        *,
        sync_run_id: int,
        reachable: bool | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await SqlAlchemySyncRunRepository(session).record_switch(sync_run_id, result)
            if reachable is not None and result.switch_id is not None:
                await SqlAlchemySwitchRepository(session).record_observation(
                    result.switch_id,
                    is_reachable=reachable,
                    checked_at=datetime.now(UTC),
                    health_error=result.error_message,
                )
            await session.commit()

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _load_switches(self, switch_ids: list[int] | None) -> tuple[Switch, ...]:
        async with self._session_factory() as session:
            repository = SqlAlchemySwitchRepository(session)
            if switch_ids:
                found = [await repository.get_by_id(switch_id) for switch_id in switch_ids]
                return tuple(switch for switch in found if switch is not None)
            page = await repository.list(
                filters=SwitchFilters(),
                page_request=PageRequest(page=1, page_size=_MAX_SWITCHES_PER_RUN),
            )
            return page.items

    async def _fail_stale_runs(self, *, now: datetime) -> None:
        cutoff = now - timedelta(minutes=self._options.stale_run_minutes)
        async with self._session_factory() as session:
            failed = await SqlAlchemySyncRunRepository(session).fail_stale_runs(older_than=cutoff)
            await session.commit()
        if failed:
            logger.warning("stale_sync_runs_failed", count=failed)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _totals(results: tuple[SwitchSyncResult, ...] | list[SwitchSyncResult]) -> SyncRunTotals:
    return SyncRunTotals(
        switches_total=len(results),
        switches_succeeded=sum(1 for r in results if r.outcome is SwitchSyncOutcome.SUCCESS),
        switches_failed=sum(1 for r in results if r.outcome is SwitchSyncOutcome.FAILED),
        switches_skipped=sum(1 for r in results if r.outcome is SwitchSyncOutcome.SKIPPED),
        vlans_discovered=sum(r.vlans_discovered for r in results),
        vlans_created=sum(r.vlans_created for r in results),
        vlans_updated=sum(r.vlans_updated for r in results),
        vlans_unchanged=sum(r.vlans_unchanged for r in results),
        vlans_marked_missing=sum(r.vlans_marked_missing for r in results),
    )


def _summarise_failures(
    results: tuple[SwitchSyncResult, ...] | list[SwitchSyncResult],
) -> str | None:
    """One-line summary naming the failed switches, for the run record."""
    failed = [r for r in results if r.outcome is SwitchSyncOutcome.FAILED]
    if not failed:
        return None
    names = ", ".join(sorted(r.switch_name for r in failed))
    return f"{len(failed)} switch(es) failed: {names}. See per-switch detail for reasons."
