"""Driver contract, DTO validation, mock driver and registry."""

from __future__ import annotations

import pytest

from nas.domain.enums import Vendor
from nas.drivers.base import (
    DiscoveredInterface,
    DiscoveredVlan,
    DriverAuthenticationError,
    DriverConnectionError,
    DriverError,
    DriverNotSupportedError,
    DriverParseError,
    NetworkDeviceDriver,
)
from nas.drivers.mock import MockDriver
from nas.drivers.registry import create_driver, open_driver, supported_vendors
from tests.fakes import make_switch


class TestDiscoveredVlanValidation:
    """Validation at the boundary: a nonsense value must fail on ingest, not
    reach the database and trip a constraint mid-transaction."""

    @pytest.mark.parametrize("tag", [1, 100, 4094])
    def test_accepts_assignable_range(self, tag: int) -> None:
        assert DiscoveredVlan(vlan_id=tag).vlan_id == tag

    @pytest.mark.parametrize("tag", [0, 4095, -1, 9999])
    def test_rejects_reserved_and_out_of_range(self, tag: int) -> None:
        with pytest.raises(DriverParseError, match="outside the assignable range"):
            DiscoveredVlan(vlan_id=tag)

    def test_rejects_duplicate_interfaces(self) -> None:
        with pytest.raises(DriverParseError, match="twice"):
            DiscoveredVlan(
                vlan_id=10,
                interfaces=(
                    DiscoveredInterface(name="ge-0/0/1"),
                    DiscoveredInterface(name="GE-0/0/1"),
                ),
            )

    def test_rejects_blank_interface_name(self) -> None:
        with pytest.raises(DriverParseError, match="blank"):
            DiscoveredInterface(name="   ")

    def test_raw_payload_is_excluded_from_repr(self) -> None:
        vlan = DiscoveredVlan(vlan_id=10, raw={"secret-ish": "device internals"})
        assert "device internals" not in repr(vlan)


class TestErrorHierarchy:
    """Every driver failure must be catchable as DriverError — that single
    except clause is what keeps an unreadable switch from marking VLANs missing."""

    @pytest.mark.parametrize(
        "error_type",
        [
            DriverConnectionError,
            DriverAuthenticationError,
            DriverParseError,
            DriverNotSupportedError,
        ],
    )
    def test_all_derive_from_driver_error(self, error_type: type[Exception]) -> None:
        assert issubclass(error_type, DriverError)


class TestMockDriver:
    async def test_satisfies_the_driver_protocol(self) -> None:
        assert isinstance(MockDriver(make_switch(vendor=Vendor.MOCK)), NetworkDeviceDriver)

    async def test_returns_vlans_after_connecting(self) -> None:
        driver = MockDriver(make_switch(name="sw-a", vendor=Vendor.MOCK))
        await driver.connect()
        vlans = await driver.get_vlans()
        await driver.close()
        assert vlans
        assert all(1 <= v.vlan_id <= 4094 for v in vlans)

    async def test_is_deterministic_across_instances(self) -> None:
        """Determinism matters: reconciliation tests must not be flaky, and a
        second sync of an unchanged device must report no changes."""

        async def tags(name: str) -> list[int]:
            driver = MockDriver(make_switch(name=name, vendor=Vendor.MOCK))
            await driver.connect()
            result = sorted(v.vlan_id for v in await driver.get_vlans())
            await driver.close()
            return result

        assert await tags("sw-a") == await tags("sw-a")

    async def test_different_switches_diverge(self) -> None:
        """Subsets must be independent per switch, or cross-switch aggregation
        (the whole point of /vlans/lookup) is never exercised."""

        async def tags(name: str) -> set[int]:
            driver = MockDriver(make_switch(name=name, vendor=Vendor.MOCK))
            await driver.connect()
            result = {v.vlan_id for v in await driver.get_vlans()}
            await driver.close()
            return result

        a, b = await tags("sw-alpha"), await tags("sw-beta")
        assert a != b
        assert a - b and b - a, "neither switch may be a strict subset of the other"

    async def test_reads_before_connect_are_refused(self) -> None:
        driver = MockDriver(make_switch(vendor=Vendor.MOCK))
        with pytest.raises(DriverConnectionError, match="Not connected"):
            await driver.get_vlans()

    async def test_close_is_safe_when_never_connected(self) -> None:
        await MockDriver(make_switch(vendor=Vendor.MOCK)).close()

    async def test_facts_are_reported(self) -> None:
        driver = MockDriver(make_switch(name="sw-a", vendor=Vendor.MOCK))
        await driver.connect()
        facts = await driver.get_facts()
        await driver.close()
        assert facts.model and facts.os_version and facts.serial_number

    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ("unreachable", DriverConnectionError),
            ("badauth", DriverAuthenticationError),
            ("garbled", DriverParseError),
        ],
    )
    async def test_failure_injection(self, marker: str, expected: type[Exception]) -> None:
        driver = MockDriver(make_switch(hostname=f"10.0.0.1-{marker}", vendor=Vendor.MOCK))
        with pytest.raises(expected):
            await driver.connect()

    async def test_drift_changes_the_reported_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def tags() -> set[int]:
            driver = MockDriver(make_switch(name="sw-a", vendor=Vendor.MOCK))
            await driver.connect()
            result = {v.vlan_id for v in await driver.get_vlans()}
            await driver.close()
            return result

        baseline = await tags()
        monkeypatch.setenv("NAS_MOCK_DRIFT", "7")
        assert await tags() != baseline

    async def test_invalid_drift_value_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAS_MOCK_DRIFT", "not-a-number")
        driver = MockDriver(make_switch(name="sw-a", vendor=Vendor.MOCK))
        await driver.connect()
        assert await driver.get_vlans()
        await driver.close()


class TestRegistry:
    def test_juniper_and_mock_are_supported(self) -> None:
        assert supported_vendors() == frozenset({Vendor.JUNIPER, Vendor.MOCK})

    def test_unimplemented_vendor_is_rejected_with_guidance(self) -> None:
        with pytest.raises(DriverNotSupportedError) as exc_info:
            create_driver(make_switch(vendor=Vendor.CISCO), None)  # type: ignore[arg-type]
        message = str(exc_info.value)
        assert "cisco" in message
        assert "juniper" in message  # tells the operator what *is* supported

    def test_mock_vendor_builds_a_mock_driver(self) -> None:
        driver = create_driver(make_switch(vendor=Vendor.MOCK), None)  # type: ignore[arg-type]
        assert isinstance(driver, MockDriver)

    async def test_open_driver_closes_on_success(self) -> None:
        async with open_driver(make_switch(vendor=Vendor.MOCK), None) as driver:  # type: ignore[arg-type]
            assert await driver.get_vlans()
        with pytest.raises(DriverConnectionError):
            await driver.get_vlans()

    async def test_open_driver_closes_when_the_body_raises(self) -> None:
        """A leaked NETCONF session holds a slot on the switch."""
        captured: NetworkDeviceDriver | None = None
        with pytest.raises(RuntimeError):
            async with open_driver(make_switch(vendor=Vendor.MOCK), None) as driver:  # type: ignore[arg-type]
                captured = driver
                raise RuntimeError("boom")
        assert captured is not None
        with pytest.raises(DriverConnectionError):
            await captured.get_vlans()
