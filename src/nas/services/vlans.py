"""VLAN query use-cases, including the availability verdict.

The lookup here is the one that answers the engineer's actual question — *is VLAN
1234 free, and if not, who has it and where* — in a single call, replacing an SSH
session and a manual read of `show vlans`.

Availability is **derived**, never stored. A VLAN id is available when no active
record exists for it in the queried scope. A persisted flag would drift away from
the switches, which is exactly the failure this service exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nas.core.errors import NotFoundError, ValidationError
from nas.domain.entities import MAX_VLAN_ID, MIN_VLAN_ID, Vlan
from nas.domain.enums import VlanAvailability, VlanState
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import SyncRunRepository, VlanFilters, VlanRepository


@dataclass(frozen=True, slots=True)
class VlanUsage:
    """Where one VLAN id is in use, on a single switch."""

    vlan: Vlan
    switch_id: int
    switch_name: str | None
    switch_site: str | None
    state: VlanState


@dataclass(frozen=True, slots=True)
class VlanLookup:
    """The aggregated answer for one 802.1Q tag across every switch."""

    vlan_id: int
    availability: VlanAvailability
    active_usages: tuple[VlanUsage, ...]
    historic_usages: tuple[VlanUsage, ...]
    """Records now missing — a VLAN that used to be here. Useful context before
    reusing a tag, since it may still be configured elsewhere off-inventory."""

    data_as_of: datetime | None
    """When the most recent successful sync completed. Null if never synced."""

    is_stale: bool
    """Whether the newest data underlying this answer is older than the staleness
    threshold. The switches remain authoritative; this flags when to distrust the
    cache."""

    @property
    def is_available(self) -> bool:
        return self.availability is VlanAvailability.AVAILABLE

    @property
    def switch_count(self) -> int:
        return len(self.active_usages)


class VlanService:
    def __init__(
        self,
        *,
        repository: VlanRepository,
        sync_runs: SyncRunRepository | None = None,
        staleness_threshold: timedelta = timedelta(hours=6),
    ) -> None:
        self._repository = repository
        self._sync_runs = sync_runs
        self._staleness_threshold = staleness_threshold

    async def search(self, *, filters: VlanFilters, page_request: PageRequest) -> Page[Vlan]:
        return await self._repository.search(filters=filters, page_request=page_request)

    async def get_vlan(self, vlan_record_id: int) -> Vlan:
        vlan = await self._repository.get_by_id(vlan_record_id)
        if vlan is None:
            raise NotFoundError(
                f"No VLAN record exists with id {vlan_record_id}.",
                details={"vlan_record_id": vlan_record_id},
            )
        return vlan

    async def lookup(self, vlan_id: int, *, now: datetime | None = None) -> VlanLookup:
        """Aggregate one VLAN id across all switches and return a verdict."""
        now = now or datetime.now(UTC)

        if vlan_id < 0 or vlan_id > 4095:
            raise ValidationError(
                f"VLAN id must be between 0 and 4095, got {vlan_id}.",
                details={"vlan_id": vlan_id},
            )

        # 0 and 4095 are reserved by 802.1Q and can never be allocated, so they
        # are reported as reserved rather than "available" — which would invite an
        # engineer to try assigning one.
        if not MIN_VLAN_ID <= vlan_id <= MAX_VLAN_ID:
            return VlanLookup(
                vlan_id=vlan_id,
                availability=VlanAvailability.RESERVED,
                active_usages=(),
                historic_usages=(),
                data_as_of=None,
                is_stale=False,
            )

        records = await self._repository.find_by_tag(vlan_id)
        active = tuple(_usage(record) for record in records if record.state is VlanState.ACTIVE)
        historic = tuple(_usage(record) for record in records if record.state is VlanState.MISSING)

        data_as_of = await self._last_successful_sync()
        return VlanLookup(
            vlan_id=vlan_id,
            availability=(VlanAvailability.IN_USE if active else VlanAvailability.AVAILABLE),
            active_usages=active,
            historic_usages=historic,
            data_as_of=data_as_of,
            is_stale=self._is_stale(data_as_of, now=now),
        )

    async def _last_successful_sync(self) -> datetime | None:
        if self._sync_runs is None:
            return None
        latest = await self._sync_runs.latest()
        if latest is None:
            return None
        # A partial run still refreshed the switches it reached, so it counts.
        return latest.finished_at

    def _is_stale(self, data_as_of: datetime | None, *, now: datetime) -> bool:
        """Never synced counts as stale: there is no evidence the data is current."""
        if data_as_of is None:
            return True
        return (now - data_as_of) > self._staleness_threshold


def _usage(record: Vlan) -> VlanUsage:
    return VlanUsage(
        vlan=record,
        switch_id=record.switch_id,
        switch_name=record.switch_name,
        switch_site=record.switch_site,
        state=record.state,
    )
