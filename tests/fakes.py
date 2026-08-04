"""In-memory test doubles.

These implement the repository Protocols, which is what lets the entire API
surface be tested without PostgreSQL. Tests that must exercise real SQL live in
tests/integration and run against a live database.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from nas.core.credentials import AuthMethod, CredentialNotFoundError, DeviceCredential
from nas.domain.entities import (
    ApiKey,
    Switch,
    SyncRun,
    SyncRunSwitch,
    Vlan,
    VlanInterface,
)
from nas.domain.enums import (
    SwitchSyncOutcome,
    SyncStatus,
    SyncTrigger,
    Vendor,
    VlanState,
)
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import (
    NewApiKey,
    NewSwitch,
    SwitchFilters,
    VlanFilters,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def make_switch(
    *,
    switch_id: int = 1,
    name: str = "adc-core-sw1",
    hostname: str = "10.20.0.11",
    vendor: Vendor = Vendor.JUNIPER,
    credential_ref: str = "juniper-core",
    site: str | None = "ADC NBO",
    environment: str | None = "production",
    is_active: bool = True,
    description: str | None = None,
) -> Switch:
    return Switch(
        id=switch_id,
        name=name,
        hostname=hostname,
        port=22,
        vendor=vendor,
        credential_ref=credential_ref,
        is_active=is_active,
        created_at=EPOCH,
        updated_at=EPOCH,
        site=site,
        environment=environment,
        description=description,
    )


class InMemorySwitchRepository:
    def __init__(self, switches: list[Switch] | None = None) -> None:
        self._switches: dict[int, Switch] = {s.id: s for s in (switches or [])}
        self._next_id = max(self._switches, default=0) + 1

    def seed(self, switch: Switch) -> None:
        self._switches[switch.id] = switch
        self._next_id = max(self._next_id, switch.id + 1)

    async def get_by_id(self, switch_id: int) -> Switch | None:
        return self._switches.get(switch_id)

    async def get_by_name(self, name: str) -> Switch | None:
        target = name.strip().lower()
        return next((s for s in self._switches.values() if s.name.lower() == target), None)

    async def list(self, *, filters: SwitchFilters, page_request: PageRequest) -> Page[Switch]:
        matches = [s for s in self._switches.values() if _matches(s, filters)]
        matches.sort(key=lambda s: s.name)
        window = matches[page_request.offset : page_request.offset + page_request.limit]
        return Page(
            items=tuple(window),
            total=len(matches),
            page=page_request.page,
            page_size=page_request.page_size,
        )

    async def create(self, data: NewSwitch) -> Switch:
        switch = Switch(
            id=self._next_id,
            name=data.name,
            hostname=data.hostname,
            port=data.port,
            vendor=data.vendor,
            credential_ref=data.credential_ref,
            is_active=data.is_active,
            created_at=EPOCH,
            updated_at=EPOCH,
            site=data.site,
            environment=data.environment,
            description=data.description,
        )
        self._switches[switch.id] = switch
        self._next_id += 1
        return switch


def _matches(switch: Switch, filters: SwitchFilters) -> bool:
    if filters.vendor is not None and switch.vendor is not filters.vendor:
        return False
    if filters.site and (switch.site or "").lower() != filters.site.lower():
        return False
    if filters.environment and (switch.environment or "").lower() != filters.environment.lower():
        return False
    if filters.is_active is not None and switch.is_active is not filters.is_active:
        return False
    if filters.search:
        term = filters.search.lower()
        haystack = " ".join(
            filter(None, (switch.name, switch.hostname, switch.description))
        ).lower()
        if term not in haystack:
            return False
    return True


class InMemoryApiKeyRepository:
    def __init__(self, keys: list[ApiKey] | None = None) -> None:
        self._keys: dict[str, ApiKey] = {k.prefix: k for k in (keys or [])}
        self._next_id = max((k.id for k in self._keys.values()), default=0) + 1
        self.marked_used: list[tuple[int, datetime]] = []

    def seed(self, key: ApiKey) -> None:
        self._keys[key.prefix] = key
        self._next_id = max(self._next_id, key.id + 1)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        return self._keys.get(prefix)

    async def list_all(self) -> tuple[ApiKey, ...]:
        return tuple(sorted(self._keys.values(), key=lambda k: k.name))

    async def create(self, data: NewApiKey) -> ApiKey:
        key = ApiKey(
            id=self._next_id,
            name=data.name,
            prefix=data.prefix,
            key_hash=data.key_hash,
            scopes=data.scopes,
            is_active=True,
            created_at=EPOCH,
            expires_at=data.expires_at,
            description=data.description,
        )
        self._keys[key.prefix] = key
        self._next_id += 1
        return key

    async def mark_used(self, api_key_id: int, *, when: datetime) -> None:
        self.marked_used.append((api_key_id, when))

    async def revoke(self, name: str) -> bool:
        for prefix, key in self._keys.items():
            if key.name == name and key.is_active:
                self._keys[prefix] = ApiKey(
                    id=key.id,
                    name=key.name,
                    prefix=key.prefix,
                    key_hash=key.key_hash,
                    scopes=key.scopes,
                    is_active=False,
                    created_at=key.created_at,
                    expires_at=key.expires_at,
                    last_used_at=key.last_used_at,
                    description=key.description,
                )
                return True
        return False


class FakeCredentialProvider:
    """Resolves a fixed set of references. Used to assert credential_status."""

    def __init__(self, refs: set[str] | None = None) -> None:
        self._refs = refs if refs is not None else {"juniper-core"}

    def get(self, ref: str) -> DeviceCredential:
        if ref not in self._refs:
            raise CredentialNotFoundError(ref)
        return DeviceCredential(
            ref=ref,
            username="nas-readonly",
            auth_method=AuthMethod.PASSWORD,
            password="unused-in-tests",
        )

    def has(self, ref: str) -> bool:
        return ref in self._refs

    def refs(self) -> frozenset[str]:
        return frozenset(self._refs)


class FakeDatabase:
    """Stands in for db.session.Database in API tests."""

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy
        self.dispose_calls = 0

    async def check(self) -> bool:
        return self._healthy

    async def dispose(self) -> None:
        self.dispose_calls += 1


def make_vlan(
    *,
    record_id: int = 1,
    switch_id: int = 1,
    vlan_id: int = 100,
    name: str | None = "sip-angani",
    description: str | None = None,
    state: VlanState = VlanState.ACTIVE,
    interfaces: tuple[VlanInterface, ...] = (),
    switch_name: str | None = "adc-core-sw1",
    switch_site: str | None = "ADC NBO",
    seen_at: datetime | None = None,
) -> Vlan:
    moment = seen_at or EPOCH
    return Vlan(
        id=record_id,
        switch_id=switch_id,
        vlan_id=vlan_id,
        state=state,
        first_seen_at=moment,
        last_seen_at=moment,
        last_synced_at=moment,
        created_at=moment,
        updated_at=moment,
        name=name,
        description=description,
        interfaces=interfaces,
        switch_name=switch_name,
        switch_site=switch_site,
    )


class InMemoryVlanRepository:
    def __init__(self, vlans: list[Vlan] | None = None) -> None:
        self._vlans: dict[int, Vlan] = {v.id: v for v in (vlans or [])}
        self.applied_plans: list[object] = []

    def seed(self, vlan: Vlan) -> None:
        self._vlans[vlan.id] = vlan

    async def get_by_id(self, vlan_record_id: int) -> Vlan | None:
        return self._vlans.get(vlan_record_id)

    async def list_for_switch(self, switch_id: int) -> tuple[Vlan, ...]:
        return tuple(v for v in self._vlans.values() if v.switch_id == switch_id)

    async def find_by_tag(self, vlan_id: int) -> tuple[Vlan, ...]:
        return tuple(
            sorted(
                (v for v in self._vlans.values() if v.vlan_id == vlan_id),
                key=lambda v: v.switch_name or "",
            )
        )

    async def search(self, *, filters: VlanFilters, page_request: PageRequest) -> Page[Vlan]:
        matches = [v for v in self._vlans.values() if _vlan_matches(v, filters)]
        matches.sort(key=lambda v: (v.vlan_id, v.switch_name or ""))
        window = matches[page_request.offset : page_request.offset + page_request.limit]
        return Page(
            items=tuple(window),
            total=len(matches),
            page=page_request.page,
            page_size=page_request.page_size,
        )

    async def apply_plan(self, plan: object, *, observed_at: datetime) -> None:
        self.applied_plans.append(plan)


def _vlan_matches(vlan: Vlan, filters: VlanFilters) -> bool:
    if filters.vlan_id is not None and vlan.vlan_id != filters.vlan_id:
        return False
    if filters.switch_id is not None and vlan.switch_id != filters.switch_id:
        return False
    if filters.state is not None and vlan.state is not filters.state:
        return False
    if filters.site and (vlan.switch_site or "").lower() != filters.site.lower():
        return False
    if filters.search:
        term = filters.search.lower()
        haystack = " ".join(
            filter(None, [vlan.name, vlan.description, *(i.name for i in vlan.interfaces)])
        ).lower()
        if term not in haystack:
            return False
    return True


class InMemorySyncRunRepository:
    def __init__(self, runs: list[SyncRun] | None = None) -> None:
        self._runs: dict[int, SyncRun] = {r.id: r for r in (runs or [])}
        self._next_id = max(self._runs, default=0) + 1
        self.stale_failed = 0

    def seed(self, run: SyncRun) -> None:
        self._runs[run.id] = run
        self._next_id = max(self._next_id, run.id + 1)

    async def start(
        self, *, trigger: SyncTrigger, correlation_id: str, started_at: datetime
    ) -> SyncRun:
        run = SyncRun(
            id=self._next_id,
            trigger=trigger,
            status=SyncStatus.RUNNING,
            started_at=started_at,
            correlation_id=correlation_id,
        )
        self._runs[run.id] = run
        self._next_id += 1
        return run

    async def record_switch(self, sync_run_id: int, result: object) -> None:
        return None

    async def finish(
        self,
        sync_run_id: int,
        *,
        status: SyncStatus,
        finished_at: datetime,
        duration_ms: int,
        totals: object,
        error_message: str | None = None,
    ) -> SyncRun:
        existing = self._runs[sync_run_id]
        updated = replace(
            existing,
            status=status,
            finished_at=finished_at,
            duration_ms=duration_ms,
            error_message=error_message,
        )
        self._runs[sync_run_id] = updated
        return updated

    async def get_by_id(self, sync_run_id: int) -> SyncRun | None:
        return self._runs.get(sync_run_id)

    async def latest(self) -> SyncRun | None:
        if not self._runs:
            return None
        return max(self._runs.values(), key=lambda r: (r.started_at, r.id))

    async def list(self, *, page_request: PageRequest) -> Page[SyncRun]:
        ordered = sorted(self._runs.values(), key=lambda r: (r.started_at, r.id), reverse=True)
        window = ordered[page_request.offset : page_request.offset + page_request.limit]
        return Page(
            items=tuple(window),
            total=len(ordered),
            page=page_request.page,
            page_size=page_request.page_size,
        )

    async def fail_stale_runs(self, *, older_than: datetime) -> int:
        return self.stale_failed


class FakeSyncService:
    """Stands in for SyncService in API tests.

    The real service owns a session factory and drives device I/O; the endpoint's
    job is only to call it, translate the result, and surface a 409 when a run is
    already in progress.
    """

    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.calls: list[dict[str, object]] = []
        self._next_id = 1

    async def run(
        self,
        *,
        trigger: SyncTrigger,
        correlation_id: str | None = None,
        switch_ids: list[int] | None = None,
    ) -> SyncRun:
        from nas.services.sync import SyncAlreadyRunningError

        self.calls.append({"trigger": trigger, "switch_ids": switch_ids})
        if self.conflict:
            raise SyncAlreadyRunningError

        run = SyncRun(
            id=self._next_id,
            trigger=trigger,
            status=SyncStatus.PARTIAL,
            started_at=EPOCH,
            correlation_id=correlation_id or "test-correlation",
            finished_at=EPOCH,
            duration_ms=1234,
            switches_total=3,
            switches_succeeded=2,
            switches_failed=1,
            switches_skipped=0,
            vlans_discovered=12,
            vlans_created=3,
            vlans_updated=2,
            vlans_unchanged=7,
            vlans_marked_missing=1,
            error_message="1 switch(es) failed: sw-c.",
            switch_results=(
                SyncRunSwitch(
                    id=1,
                    sync_run_id=self._next_id,
                    switch_id=1,
                    switch_name="sw-a",
                    outcome=SwitchSyncOutcome.SUCCESS,
                    vlans_discovered=12,
                ),
                SyncRunSwitch(
                    id=2,
                    sync_run_id=self._next_id,
                    switch_id=3,
                    switch_name="sw-c",
                    outcome=SwitchSyncOutcome.FAILED,
                    error_message="Simulated: host unreachable",
                ),
            ),
        )
        self._next_id += 1
        return run
