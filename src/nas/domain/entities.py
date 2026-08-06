"""Domain entities.

Immutable, framework-free representations of the concepts this service manages.
Repositories translate persistence rows into these; services and the API layer
work with these rather than ORM objects, which keeps business rules testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from nas.domain.enums import (
    CredentialStatus,
    InterfaceMode,
    ReachabilityState,
    SwitchSyncOutcome,
    SyncStatus,
    SyncTrigger,
    Vendor,
    VlanState,
)

# 802.1Q: 0 and 4095 are reserved, leaving 1-4094 assignable.
MIN_VLAN_ID = 1
MAX_VLAN_ID = 4094


@dataclass(frozen=True, slots=True)
class Switch:
    """A managed network device."""

    id: int
    name: str
    hostname: str
    port: int
    vendor: Vendor
    credential_ref: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    site: str | None = None
    environment: str | None = None
    model: str | None = None
    os_version: str | None = None
    description: str | None = None
    is_reachable: bool | None = None
    last_health_check: datetime | None = None
    health_error: str | None = None

    @property
    def reachability(self) -> ReachabilityState:
        if self.is_reachable is None:
            return ReachabilityState.UNKNOWN
        return ReachabilityState.REACHABLE if self.is_reachable else ReachabilityState.UNREACHABLE

    def credential_status(self, *, store_configured: bool, resolvable: bool) -> CredentialStatus:
        """Classify this switch's credential without exposing the credential.

        Distinguishes "no credential store configured at all" from "store exists
        but this reference is absent", because the two have different fixes.
        """
        if not store_configured:
            return CredentialStatus.NOT_CONFIGURED
        return CredentialStatus.RESOLVED if resolvable else CredentialStatus.MISSING


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A credential issued to an API consumer, e.g. ClientManager."""

    id: int
    name: str
    prefix: str
    key_hash: str = field(repr=False)
    scopes: frozenset[str]
    is_active: bool
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    description: str | None = None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at

    def is_usable(self, *, now: datetime | None = None) -> bool:
        return self.is_active and not self.is_expired(now=now)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_all_scopes(self, required: frozenset[str]) -> bool:
        return required.issubset(self.scopes)


@dataclass(frozen=True, slots=True)
class VlanInterface:
    """An interface that carries a VLAN — the "where is it used" detail.

    Reading port membership is discovery, not configuration; nothing in Phase 1
    modifies interfaces.
    """

    name: str
    mode: InterfaceMode = InterfaceMode.UNKNOWN
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Vlan:
    """A VLAN as discovered on one switch.

    Identity is ``(switch_id, vlan_id)``, not ``vlan_id`` alone: the same VLAN id
    legitimately exists on several switches, and answering "who owns 1234" means
    aggregating across all of them.
    """

    id: int
    switch_id: int
    vlan_id: int
    state: VlanState
    first_seen_at: datetime
    last_seen_at: datetime
    last_synced_at: datetime
    created_at: datetime
    updated_at: datetime
    name: str | None = None
    description: str | None = None
    l3_interface: str | None = None
    vxlan_vni: int | None = None
    interfaces: tuple[VlanInterface, ...] = ()
    # Denormalised for display so listing VLANs does not require a join per row.
    switch_name: str | None = None
    switch_site: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state is VlanState.ACTIVE

    @property
    def interface_count(self) -> int:
        return len(self.interfaces)


@dataclass(frozen=True, slots=True)
class SyncRunSwitch:
    """Per-switch detail within a run.

    Without this, a run touching four switches where one failed could only be
    recorded as an average. Attribution has to survive to the operator.
    """

    id: int
    sync_run_id: int
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

    @property
    def succeeded(self) -> bool:
        return self.outcome is SwitchSyncOutcome.SUCCESS


@dataclass(frozen=True, slots=True)
class SyncRun:
    """One synchronisation pass over the configured switches."""

    id: int
    trigger: SyncTrigger
    status: SyncStatus
    started_at: datetime
    correlation_id: str
    finished_at: datetime | None = None
    duration_ms: int | None = None
    switches_total: int = 0
    switches_succeeded: int = 0
    switches_failed: int = 0
    switches_skipped: int = 0
    vlans_discovered: int = 0
    vlans_created: int = 0
    vlans_updated: int = 0
    vlans_unchanged: int = 0
    vlans_marked_missing: int = 0
    error_message: str | None = None
    switch_results: tuple[SyncRunSwitch, ...] = ()

    @property
    def is_running(self) -> bool:
        return self.status is SyncStatus.RUNNING

    @staticmethod
    def derive_status(*, succeeded: int, failed: int, skipped: int, attempted: int) -> SyncStatus:
        """Classify a finished run from its per-switch outcomes.

        Skipped switches are excluded from the verdict: a switch deliberately
        marked inactive is not a failure. A run with nothing to attempt is a
        success — there was no work and nothing went wrong.
        """
        del skipped  # deliberately not part of the verdict
        if attempted == 0:
            return SyncStatus.SUCCESS
        if failed == 0:
            return SyncStatus.SUCCESS
        if succeeded == 0:
            return SyncStatus.FAILED
        return SyncStatus.PARTIAL
