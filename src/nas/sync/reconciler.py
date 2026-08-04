"""VLAN reconciliation — pure diffing logic.

Given what a switch currently reports and what the database already holds, decide
what to create, update, leave alone and mark missing. No I/O, no ORM, no driver:
just data in, plan out. That makes the riskiest logic in the service exhaustively
testable, which matters because the failure modes here are destructive.

Two safety properties are enforced structurally rather than by convention:

1. **A failed read never reaches this module.** The caller only builds a plan
   after a driver returns successfully. An unreachable switch produces no plan at
   all, so it cannot mark anything missing. See ``nas.services.sync``.
2. **An empty discovery result will not wipe a switch.** A device reporting zero
   VLANs when the database holds active ones is far more likely to be a silent
   read failure than a genuine mass deletion, so it is refused by default. See
   ``allow_empty_discovery``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from nas.domain.entities import Vlan, VlanInterface
from nas.domain.enums import VlanState
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan


class ReconciliationRefusedError(Exception):
    """Raised when a plan would be destructive in a way that looks like a bug.

    Deliberately not a DriverError: the device answered fine. The caller records
    the switch as failed and leaves existing data untouched.
    """


class VlanAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    MARKED_MISSING = "marked_missing"


# Fields whose change constitutes a real VLAN change. Timestamps are excluded —
# they always move, and treating them as changes would make every VLAN "updated"
# on every run.
_COMPARED_SCALARS = ("name", "description", "l3_interface", "vxlan_vni")


@dataclass(frozen=True, slots=True)
class VlanUpdate:
    """An existing record paired with the device's current view of it."""

    existing: Vlan
    discovered: DiscoveredVlan
    reactivated: bool = False
    """True when a previously missing VLAN has reappeared."""

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return _changed_fields(self.existing, self.discovered)


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """What to do about one switch's VLANs."""

    switch_id: int
    to_create: tuple[DiscoveredVlan, ...] = ()
    to_update: tuple[VlanUpdate, ...] = ()
    unchanged: tuple[Vlan, ...] = ()
    to_mark_missing: tuple[Vlan, ...] = ()
    already_missing: tuple[Vlan, ...] = field(default=(), repr=False)

    @property
    def discovered_count(self) -> int:
        return len(self.to_create) + len(self.to_update) + len(self.unchanged)

    @property
    def counts(self) -> dict[str, int]:
        return {
            VlanAction.CREATED.value: len(self.to_create),
            VlanAction.UPDATED.value: len(self.to_update),
            VlanAction.UNCHANGED.value: len(self.unchanged),
            VlanAction.MARKED_MISSING.value: len(self.to_mark_missing),
        }

    @property
    def has_writes(self) -> bool:
        """Whether anything at all needs persisting.

        ``unchanged`` still counts: those rows need their ``last_seen_at`` bumped,
        otherwise staleness reporting would wrongly show them as going stale.
        """
        return bool(self.to_create or self.to_update or self.unchanged or self.to_mark_missing)


def _interface_signature(
    interfaces: Sequence[VlanInterface | DiscoveredInterface],
) -> frozenset[tuple[str, str]]:
    """Order-insensitive, case-insensitive comparison key for port membership.

    Switches do not guarantee interface ordering between reads, so comparing
    sequences directly would report spurious changes.
    """
    return frozenset(
        (interface.name.strip().lower(), interface.mode.value) for interface in interfaces
    )


def _normalise(value: object) -> object:
    """Treat empty string and None as equivalent.

    Some Junos releases report an absent description as "" and others omit the
    element entirely; the difference is not a change.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _changed_fields(existing: Vlan, discovered: DiscoveredVlan) -> tuple[str, ...]:
    changed = [
        name
        for name in _COMPARED_SCALARS
        if _normalise(getattr(existing, name)) != _normalise(getattr(discovered, name))
    ]
    if _interface_signature(existing.interfaces) != _interface_signature(discovered.interfaces):
        changed.append("interfaces")
    return tuple(changed)


def build_plan(
    *,
    switch_id: int,
    existing: Sequence[Vlan],
    discovered: Sequence[DiscoveredVlan],
    allow_empty_discovery: bool = False,
) -> ReconciliationPlan:
    """Diff a switch's reported VLANs against the stored records.

    ``existing`` must be **every** stored record for the switch, active and
    missing alike — otherwise a previously missing VLAN that has reappeared would
    be misread as new and violate the unique constraint on
    ``(switch_id, vlan_id)``.

    ``discovered`` must be the **complete** current set from the device. Drivers
    guarantee this: ``get_vlans`` returns everything or raises.

    Raises ReconciliationRefusedError if the device reported nothing while active
    records exist and ``allow_empty_discovery`` is False.
    """
    existing_by_tag: dict[int, Vlan] = {}
    for existing_record in existing:
        # Defensive: a duplicate tag for one switch cannot happen given the unique
        # constraint, but silently picking one would hide a real problem.
        if existing_record.vlan_id in existing_by_tag:
            raise ReconciliationRefusedError(
                f"Switch {switch_id} has duplicate stored records for VLAN "
                f"{existing_record.vlan_id}."
            )
        existing_by_tag[existing_record.vlan_id] = existing_record

    discovered_by_tag: dict[int, DiscoveredVlan] = {}
    for item in discovered:
        # Drivers deduplicate, but a new driver might not; failing loudly here is
        # better than writing whichever row happened to come last.
        if item.vlan_id in discovered_by_tag:
            raise ReconciliationRefusedError(
                f"Device reported VLAN {item.vlan_id} twice for switch {switch_id}."
            )
        discovered_by_tag[item.vlan_id] = item

    active_existing = [record for record in existing if record.state is VlanState.ACTIVE]

    if not discovered_by_tag and active_existing and not allow_empty_discovery:
        raise ReconciliationRefusedError(
            f"Switch {switch_id} reported zero VLANs while {len(active_existing)} active "
            "records exist. Refusing to mark them all missing — this is far more likely a "
            "silent read failure than a genuine mass deletion. Set "
            "NAS_SYNC_ALLOW_EMPTY_DISCOVERY=true if the switch really has no VLANs."
        )

    to_create: list[DiscoveredVlan] = []
    to_update: list[VlanUpdate] = []
    unchanged: list[Vlan] = []

    for tag, item in discovered_by_tag.items():
        stored = existing_by_tag.get(tag)
        if stored is None:
            to_create.append(item)
            continue

        reactivated = stored.state is not VlanState.ACTIVE
        if reactivated or _changed_fields(stored, item):
            # A reappearance is an update even when every field matches: the
            # state transition itself must be persisted.
            to_update.append(VlanUpdate(existing=stored, discovered=item, reactivated=reactivated))
        else:
            unchanged.append(stored)

    to_mark_missing = [
        record for record in active_existing if record.vlan_id not in discovered_by_tag
    ]
    already_missing = [
        record
        for record in existing
        if record.state is VlanState.MISSING and record.vlan_id not in discovered_by_tag
    ]

    return ReconciliationPlan(
        switch_id=switch_id,
        to_create=tuple(to_create),
        to_update=tuple(to_update),
        unchanged=tuple(unchanged),
        to_mark_missing=tuple(to_mark_missing),
        already_missing=tuple(already_missing),
    )
