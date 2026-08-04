"""Repository interfaces.

Services depend on these Protocols, never on SQLAlchemy. Two payoffs:

1. API-level tests substitute in-memory fakes and need no database at all.
2. The persistence layer can change without touching business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nas.domain.entities import ApiKey, Switch, SyncRun, Vlan
from nas.domain.enums import (
    SwitchSyncOutcome,
    SyncStatus,
    SyncTrigger,
    Vendor,
    VlanState,
)
from nas.domain.pagination import Page, PageRequest
from nas.sync.reconciler import ReconciliationPlan


@dataclass(frozen=True, slots=True)
class SwitchFilters:
    """Query filters for the switch inventory. All fields are optional."""

    vendor: Vendor | None = None
    site: str | None = None
    environment: str | None = None
    is_active: bool | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class NewSwitch:
    """Input for creating a switch. Carries no secret — only a credential ref."""

    name: str
    hostname: str
    vendor: Vendor
    credential_ref: str
    port: int = 22
    site: str | None = None
    environment: str | None = None
    description: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class NewApiKey:
    """Input for creating an API key. ``key_hash`` is a digest, never plaintext."""

    name: str
    prefix: str
    key_hash: str
    scopes: frozenset[str]
    description: str | None = None
    expires_at: datetime | None = None


class SwitchRepository(Protocol):
    async def get_by_id(self, switch_id: int) -> Switch | None: ...

    async def get_by_name(self, name: str) -> Switch | None: ...

    async def list(self, *, filters: SwitchFilters, page_request: PageRequest) -> Page[Switch]: ...

    async def create(self, data: NewSwitch) -> Switch: ...

    async def record_observation(
        self,
        switch_id: int,
        *,
        is_reachable: bool,
        checked_at: datetime,
        health_error: str | None = None,
        model: str | None = None,
        os_version: str | None = None,
    ) -> None: ...


class ApiKeyRepository(Protocol):
    async def get_by_prefix(self, prefix: str) -> ApiKey | None: ...

    async def list_all(self) -> tuple[ApiKey, ...]: ...

    async def create(self, data: NewApiKey) -> ApiKey: ...

    async def mark_used(self, api_key_id: int, *, when: datetime) -> None: ...

    async def revoke(self, name: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class VlanFilters:
    """Query filters for discovered VLANs. All fields optional."""

    vlan_id: int | None = None
    switch_id: int | None = None
    site: str | None = None
    state: VlanState | None = None
    search: str | None = None
    """Free text over VLAN name, description and member interface name."""


@dataclass(frozen=True, slots=True)
class SwitchSyncResult:
    """What happened to one switch during a run, ready to persist."""

    switch_id: int | None
    switch_name: str
    outcome: SwitchSyncOutcome
    vlans_discovered: int = 0
    vlans_created: int = 0
    vlans_updated: int = 0
    vlans_unchanged: int = 0
    vlans_marked_missing: int = 0
    duration_ms: int | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SyncRunTotals:
    """Aggregate counters written when a run finishes."""

    switches_total: int = 0
    switches_succeeded: int = 0
    switches_failed: int = 0
    switches_skipped: int = 0
    vlans_discovered: int = 0
    vlans_created: int = 0
    vlans_updated: int = 0
    vlans_unchanged: int = 0
    vlans_marked_missing: int = 0


class VlanRepository(Protocol):
    async def get_by_id(self, vlan_record_id: int) -> Vlan | None: ...

    async def list_for_switch(self, switch_id: int) -> tuple[Vlan, ...]:
        """Every stored record for a switch, **both active and missing**.

        Reconciliation needs the missing ones too: a VLAN that reappears must be
        reactivated, not inserted, or it violates the (switch_id, vlan_id) unique
        constraint.
        """
        ...

    async def find_by_tag(self, vlan_id: int) -> tuple[Vlan, ...]:
        """Every record for one 802.1Q tag, across all switches."""
        ...

    async def search(self, *, filters: VlanFilters, page_request: PageRequest) -> Page[Vlan]: ...

    async def apply_plan(self, plan: ReconciliationPlan, *, observed_at: datetime) -> None:
        """Persist a reconciliation plan atomically."""
        ...


class SyncRunRepository(Protocol):
    async def start(
        self, *, trigger: SyncTrigger, correlation_id: str, started_at: datetime
    ) -> SyncRun: ...

    async def record_switch(self, sync_run_id: int, result: SwitchSyncResult) -> None: ...

    async def finish(
        self,
        sync_run_id: int,
        *,
        status: SyncStatus,
        finished_at: datetime,
        duration_ms: int,
        totals: SyncRunTotals,
        error_message: str | None = None,
    ) -> SyncRun: ...

    async def get_by_id(self, sync_run_id: int) -> SyncRun | None: ...

    async def latest(self) -> SyncRun | None: ...

    async def list(self, *, page_request: PageRequest) -> Page[SyncRun]: ...

    async def fail_stale_runs(self, *, older_than: datetime) -> int:
        """Mark long-abandoned 'running' rows as failed, returning the count.

        A process killed mid-run would otherwise leave a row 'running' forever,
        and /sync/status would report a sync in progress indefinitely.
        """
        ...
