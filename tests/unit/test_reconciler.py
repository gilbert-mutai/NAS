"""VLAN reconciliation logic.

The destructive failure modes live here, so this is the most thoroughly covered
module in the service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nas.domain.entities import Vlan, VlanInterface
from nas.domain.enums import InterfaceMode, VlanState
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan
from nas.sync.reconciler import (
    ReconciliationRefusedError,
    VlanAction,
    build_plan,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SWITCH_ID = 7


def stored(
    tag: int,
    *,
    name: str | None = "vlan",
    description: str | None = None,
    l3_interface: str | None = None,
    vxlan_vni: int | None = None,
    state: VlanState = VlanState.ACTIVE,
    interfaces: tuple[VlanInterface, ...] = (),
) -> Vlan:
    return Vlan(
        id=1000 + tag,
        switch_id=SWITCH_ID,
        vlan_id=tag,
        state=state,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_synced_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        name=name,
        description=description,
        l3_interface=l3_interface,
        vxlan_vni=vxlan_vni,
        interfaces=interfaces,
    )


def found(
    tag: int,
    *,
    name: str | None = "vlan",
    description: str | None = None,
    l3_interface: str | None = None,
    vxlan_vni: int | None = None,
    interfaces: tuple[DiscoveredInterface, ...] = (),
) -> DiscoveredVlan:
    return DiscoveredVlan(
        vlan_id=tag,
        name=name,
        description=description,
        l3_interface=l3_interface,
        vxlan_vni=vxlan_vni,
        interfaces=interfaces,
    )


def plan_for(existing: list[Vlan], discovered: list[DiscoveredVlan], **kwargs: object):
    return build_plan(
        switch_id=SWITCH_ID,
        existing=existing,
        discovered=discovered,
        **kwargs,  # type: ignore[arg-type]
    )


class TestPlanIdentity:
    """Regression guard for a silent aliasing bug.

    A leaked loop variable once made every plan entry reference the *last* stored
    record instead of the matching one. Counts still looked right, `mypy --strict`
    passed, and the effect only showed up as VLANs staying 'missing' after being
    rediscovered. These tests assert identity, not just totals.
    """

    def test_each_update_references_its_own_record(self) -> None:
        existing = [stored(t, description="old") for t in (10, 20, 30, 3000)]
        discovered = [found(t, description="new") for t in (10, 20, 30, 3000)]

        plan = plan_for(existing, discovered)

        assert sorted(u.existing.vlan_id for u in plan.to_update) == [10, 20, 30, 3000]
        # Each pairing must be self-consistent, not merely present.
        for update in plan.to_update:
            assert update.existing.vlan_id == update.discovered.vlan_id

    def test_each_unchanged_references_its_own_record(self) -> None:
        existing = [stored(t) for t in (10, 20, 30, 3000)]
        discovered = [found(t) for t in (10, 20, 30, 3000)]

        plan = plan_for(existing, discovered)

        assert sorted(v.vlan_id for v in plan.unchanged) == [10, 20, 30, 3000]
        assert len({v.id for v in plan.unchanged}) == 4  # four distinct records

    def test_plan_partitions_the_discovered_set_exactly(self) -> None:
        existing = [stored(10), stored(20, state=VlanState.MISSING), stored(99)]
        discovered = [found(10), found(20), found(30)]

        plan = plan_for(existing, discovered)

        covered = sorted(
            [v.vlan_id for v in plan.to_create]
            + [u.existing.vlan_id for u in plan.to_update]
            + [v.vlan_id for v in plan.unchanged]
        )
        assert covered == [10, 20, 30]
        assert [v.vlan_id for v in plan.to_mark_missing] == [99]

    def test_records_are_never_in_two_buckets(self) -> None:
        existing = [stored(t) for t in (1, 2, 3, 4, 5)]
        discovered = [found(1), found(2, description="changed"), found(9)]

        plan = plan_for(existing, discovered)

        ids = (
            [u.existing.id for u in plan.to_update]
            + [v.id for v in plan.unchanged]
            + [v.id for v in plan.to_mark_missing]
        )
        assert len(ids) == len(set(ids))


class TestCreate:
    def test_unknown_vlan_is_created(self) -> None:
        plan = plan_for([], [found(100)])
        assert [v.vlan_id for v in plan.to_create] == [100]
        assert plan.counts[VlanAction.CREATED.value] == 1

    def test_created_count_excludes_existing(self) -> None:
        plan = plan_for([stored(100)], [found(100), found(200)])
        assert [v.vlan_id for v in plan.to_create] == [200]


class TestChangeDetection:
    @pytest.mark.parametrize(
        ("field", "old", "new"),
        [
            ("name", "old-name", "new-name"),
            ("description", "old desc", "new desc"),
            ("l3_interface", "irb.10", "irb.11"),
            ("vxlan_vni", 10010, 10011),
        ],
    )
    def test_scalar_change_marks_updated(self, field: str, old: object, new: object) -> None:
        plan = plan_for(
            [stored(10, **{field: old})],  # type: ignore[arg-type]
            [found(10, **{field: new})],  # type: ignore[arg-type]
        )
        assert len(plan.to_update) == 1
        assert field in plan.to_update[0].changed_fields

    def test_identical_record_is_unchanged(self) -> None:
        plan = plan_for(
            [stored(10, name="a", description="b", l3_interface="irb.10")],
            [found(10, name="a", description="b", l3_interface="irb.10")],
        )
        assert len(plan.unchanged) == 1
        assert not plan.to_update

    @pytest.mark.parametrize(("old", "new"), [("", None), (None, ""), ("  ", None), ("x", " x ")])
    def test_blank_and_none_are_equivalent(self, old: str | None, new: str | None) -> None:
        """Junos reports an absent description as "" on some releases and omits it
        on others. That difference is not a change."""
        plan = plan_for([stored(10, description=old)], [found(10, description=new)])
        changed = plan.to_update[0].changed_fields if plan.to_update else ()
        assert len(plan.unchanged) == 1, changed

    def test_timestamps_are_not_compared(self) -> None:
        """Otherwise every VLAN would be 'updated' on every run."""
        record = stored(10)
        object.__setattr__(record, "last_seen_at", datetime(2020, 1, 1, tzinfo=UTC))
        plan = plan_for([record], [found(10)])
        assert len(plan.unchanged) == 1


class TestInterfaceComparison:
    def test_reordered_interfaces_are_not_a_change(self) -> None:
        existing = (
            VlanInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),
            VlanInterface(name="ge-0/0/2", mode=InterfaceMode.TRUNK),
        )
        discovered = (
            DiscoveredInterface(name="ge-0/0/2", mode=InterfaceMode.TRUNK),
            DiscoveredInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),
        )
        plan = plan_for([stored(10, interfaces=existing)], [found(10, interfaces=discovered)])
        assert len(plan.unchanged) == 1

    def test_interface_case_is_ignored(self) -> None:
        plan = plan_for(
            [stored(10, interfaces=(VlanInterface(name="ge-0/0/1", mode=InterfaceMode.ACCESS),))],
            [
                found(
                    10,
                    interfaces=(DiscoveredInterface(name="GE-0/0/1", mode=InterfaceMode.ACCESS),),
                )
            ],
        )
        assert len(plan.unchanged) == 1

    def test_added_interface_is_a_change(self) -> None:
        plan = plan_for(
            [stored(10, interfaces=(VlanInterface(name="ge-0/0/1"),))],
            [
                found(
                    10,
                    interfaces=(
                        DiscoveredInterface(name="ge-0/0/1"),
                        DiscoveredInterface(name="ge-0/0/2"),
                    ),
                )
            ],
        )
        assert "interfaces" in plan.to_update[0].changed_fields

    def test_mode_change_is_a_change(self) -> None:
        plan = plan_for(
            [stored(10, interfaces=(VlanInterface(name="ge-0/0/1", mode=InterfaceMode.ACCESS),))],
            [
                found(
                    10, interfaces=(DiscoveredInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),)
                )
            ],
        )
        assert "interfaces" in plan.to_update[0].changed_fields

    def test_removed_interface_is_a_change(self) -> None:
        plan = plan_for(
            [
                stored(
                    10,
                    interfaces=(
                        VlanInterface(name="ge-0/0/1"),
                        VlanInterface(name="ge-0/0/2"),
                    ),
                )
            ],
            [found(10, interfaces=(DiscoveredInterface(name="ge-0/0/1"),))],
        )
        assert "interfaces" in plan.to_update[0].changed_fields


class TestSoftRemoval:
    def test_absent_vlan_is_marked_missing(self) -> None:
        plan = plan_for([stored(10), stored(20)], [found(10)])
        assert [v.vlan_id for v in plan.to_mark_missing] == [20]

    def test_already_missing_vlan_is_not_remarked(self) -> None:
        plan = plan_for([stored(20, state=VlanState.MISSING)], [found(10)])
        assert plan.to_mark_missing == ()
        assert [v.vlan_id for v in plan.already_missing] == [20]

    def test_reappearing_vlan_is_reactivated(self) -> None:
        plan = plan_for([stored(10, state=VlanState.MISSING)], [found(10)])
        assert len(plan.to_update) == 1
        assert plan.to_update[0].reactivated is True

    def test_reappearance_is_an_update_even_when_identical(self) -> None:
        """The state transition itself must be persisted, so it cannot be
        classified 'unchanged' just because no field differs."""
        plan = plan_for(
            [stored(10, name="same", state=VlanState.MISSING)], [found(10, name="same")]
        )
        assert len(plan.to_update) == 1
        assert plan.unchanged == ()
        assert plan.to_update[0].changed_fields == ()

    def test_active_vlan_is_not_flagged_reactivated(self) -> None:
        plan = plan_for([stored(10, description="a")], [found(10, description="b")])
        assert plan.to_update[0].reactivated is False


class TestMassRemovalGuard:
    def test_empty_discovery_with_active_records_is_refused(self) -> None:
        with pytest.raises(ReconciliationRefusedError, match="zero VLANs"):
            plan_for([stored(10), stored(20)], [])

    def test_refusal_names_the_override(self) -> None:
        with pytest.raises(ReconciliationRefusedError, match="NAS_SYNC_ALLOW_EMPTY_DISCOVERY"):
            plan_for([stored(10)], [])

    def test_override_permits_wiping(self) -> None:
        plan = plan_for([stored(10)], [], allow_empty_discovery=True)
        assert [v.vlan_id for v in plan.to_mark_missing] == [10]

    def test_empty_discovery_with_no_records_is_fine(self) -> None:
        assert plan_for([], []).counts == {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "marked_missing": 0,
        }

    def test_empty_discovery_with_only_missing_records_is_fine(self) -> None:
        """Nothing would be destroyed, so there is nothing to guard against."""
        plan = plan_for([stored(10, state=VlanState.MISSING)], [])
        assert plan.to_mark_missing == ()


class TestIntegrityGuards:
    def test_duplicate_stored_record_is_refused(self) -> None:
        with pytest.raises(ReconciliationRefusedError, match="duplicate stored records"):
            plan_for([stored(10), stored(10)], [found(10)])

    def test_duplicate_discovered_vlan_is_refused(self) -> None:
        with pytest.raises(ReconciliationRefusedError, match="twice"):
            plan_for([], [found(10), found(10)])


class TestPlanSummary:
    def test_discovered_count_covers_all_seen_vlans(self) -> None:
        plan = plan_for(
            [stored(10), stored(20, description="old"), stored(99)],
            [found(10), found(20, description="new"), found(30)],
        )
        assert plan.discovered_count == 3
        assert plan.counts == {
            "created": 1,
            "updated": 1,
            "unchanged": 1,
            "marked_missing": 1,
        }

    def test_has_writes_true_for_unchanged_only(self) -> None:
        """Unchanged rows still need last_seen_at bumped."""
        plan = plan_for([stored(10)], [found(10)])
        assert plan.unchanged
        assert plan.has_writes is True

    def test_has_writes_false_for_an_empty_plan(self) -> None:
        assert plan_for([], []).has_writes is False
