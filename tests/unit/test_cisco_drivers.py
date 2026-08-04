"""Cisco driver transport behaviour.

The NX-OS driver is exercised end to end through ``httpx.MockTransport`` — real
request construction, real status-code handling, real parsing — with no device and
no network. The IOS-XE driver's netmiko dependency is absent here, which is itself
worth asserting: the optional extra must fail with actionable guidance rather than
an ImportError traceback.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nas.core.credentials import AuthMethod, DeviceCredential
from nas.domain.enums import Vendor
from nas.drivers.base import (
    DriverAuthenticationError,
    DriverConnectionError,
    DriverDependencyError,
    DriverError,
    DriverParseError,
    NetworkDeviceDriver,
)
from nas.drivers.cisco_iosxe import CiscoIosXeDriver
from nas.drivers.cisco_nxos import CiscoNxosDriver
from nas.drivers.options import DriverOptions
from tests.fakes import make_switch

PASSWORD_CREDENTIAL = DeviceCredential(
    ref="cisco-ro",
    username="nas-readonly",
    auth_method=AuthMethod.PASSWORD,
    password="unused-in-tests",
)

KEY_CREDENTIAL = DeviceCredential(
    ref="cisco-key",
    username="nas-readonly",
    auth_method=AuthMethod.SSH_KEY,
    private_key_path="/etc/nas/keys/id_ed25519",  # type: ignore[arg-type]
)

ENABLE_CREDENTIAL = DeviceCredential(
    ref="cisco-enable",
    username="nas-readonly",
    auth_method=AuthMethod.PASSWORD,
    password="unused-in-tests",
    enable_password="enable-secret",
)

VLAN_BODY: dict[str, Any] = {
    "TABLE_vlanbriefxbrief": {
        "ROW_vlanbriefxbrief": [
            {
                "vlanshowbr-vlanid": "110",
                "vlanshowbr-vlanname": "sip-safaricom",
                "vlanshowbr-vlanstate": "active",
                "vlanshowplist-ifidx": "Ethernet1/10",
            }
        ]
    }
}

VERSION_BODY: dict[str, Any] = {
    "chassis_id": "Nexus9000 C93180YC-EX chassis",
    "nxos_ver_str": "9.3(10)",
}

RUNNING_CONFIG = "vlan 110\n  name sip-safaricom\n  vn-segment 10110\n"

SVI_BODY: dict[str, Any] = {"TABLE_interface": {"ROW_interface": [{"interface": "Vlan110"}]}}


def wrap(body: Any) -> dict[str, Any]:
    return {
        "ins_api": {
            "version": "1.0",
            "outputs": {"output": {"code": "200", "msg": "Success", "body": body}},
        }
    }


def nxos_driver(
    handler: Any,
    *,
    switch_port: int = 443,
    credential: DeviceCredential = PASSWORD_CREDENTIAL,
) -> CiscoNxosDriver:
    """Build a driver whose HTTP client is a MockTransport, bypassing connect()."""
    switch = make_switch(vendor=Vendor.CISCO_NXOS, hostname="10.90.0.5")
    object.__setattr__(switch, "port", switch_port)
    driver = CiscoNxosDriver(switch, credential, DriverOptions())
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://10.90.0.5:443",
    )
    return driver


def route(responses: dict[str, Any]) -> Any:
    """Dispatch on the NX-API 'input' command in the request body."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        command = payload["ins_api"]["input"]
        for prefix, response in responses.items():
            if command.startswith(prefix):
                if isinstance(response, httpx.Response):
                    return response
                return httpx.Response(200, json=wrap(response))
        return httpx.Response(200, json=wrap({}))

    return handler


class TestNxosProtocolConformance:
    def test_satisfies_the_driver_protocol(self) -> None:
        driver = CiscoNxosDriver(
            make_switch(vendor=Vendor.CISCO_NXOS), PASSWORD_CREDENTIAL, DriverOptions()
        )
        assert isinstance(driver, NetworkDeviceDriver)


class TestNxosConfigurationGuards:
    async def test_port_22_is_refused_with_the_fix_in_the_message(self) -> None:
        """Registering a Nexus on 22 is a mistake. Silently substituting 443 would
        connect somewhere the operator never specified."""
        switch = make_switch(vendor=Vendor.CISCO_NXOS, name="n9k-1")
        driver = CiscoNxosDriver(switch, PASSWORD_CREDENTIAL, DriverOptions())
        with pytest.raises(DriverError) as exc_info:
            await driver.connect()
        message = str(exc_info.value)
        assert "port 22" in message
        assert "--port 443" in message
        assert "n9k-1" in message

    async def test_ssh_key_auth_is_refused(self) -> None:
        """NX-API authenticates with a username and password, not a key."""
        switch = make_switch(vendor=Vendor.CISCO_NXOS)
        object.__setattr__(switch, "port", 443)
        driver = CiscoNxosDriver(switch, KEY_CREDENTIAL, DriverOptions())
        with pytest.raises(DriverError, match="username and password"):
            await driver.connect()

    async def test_reads_before_connect_are_refused(self) -> None:
        switch = make_switch(vendor=Vendor.CISCO_NXOS)
        object.__setattr__(switch, "port", 443)
        driver = CiscoNxosDriver(switch, PASSWORD_CREDENTIAL, DriverOptions())
        with pytest.raises(DriverConnectionError, match="Not connected"):
            await driver.get_vlans()


class TestNxosRequestConstruction:
    async def test_posts_the_ins_api_envelope_to_ins(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=wrap(VLAN_BODY))

        driver = nxos_driver(handler)
        await driver.get_vlans()
        await driver.close()

        assert captured[0].url.path == "/ins"
        assert captured[0].method == "POST"
        payload = json.loads(captured[0].content)["ins_api"]
        assert payload["input"] == "show vlan"
        assert payload["type"] == "cli_show"

    async def test_running_config_is_requested_as_ascii(self) -> None:
        """`show running-config vlan` returns text, not JSON."""
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)["ins_api"]
            captured.append(payload)
            return httpx.Response(200, json=wrap(VLAN_BODY))

        driver = nxos_driver(handler)
        await driver.get_vlans()
        await driver.close()

        running = [p for p in captured if p["input"].startswith("show running-config")]
        assert running and running[0]["type"] == "cli_show_ascii"


class TestNxosDiscovery:
    async def test_discovers_vlans(self) -> None:
        driver = nxos_driver(route({"show vlan": VLAN_BODY}))
        vlans = await driver.get_vlans()
        await driver.close()
        assert [v.vlan_id for v in vlans] == [110]
        assert vlans[0].name == "sip-safaricom"

    async def test_enriches_with_svi_and_vni(self) -> None:
        driver = nxos_driver(
            route(
                {
                    "show vlan": VLAN_BODY,
                    "show interface brief": SVI_BODY,
                    "show running-config vlan": RUNNING_CONFIG,
                }
            )
        )
        vlans = await driver.get_vlans()
        await driver.close()
        assert vlans[0].l3_interface == "Vlan110"
        assert vlans[0].vxlan_vni == 10110

    async def test_missing_enrichment_still_yields_vlans(self) -> None:
        """A network-operator role may not permit `show running-config vlan`.
        VLANs must still sync — just without VNIs."""

        def handler(request: httpx.Request) -> httpx.Response:
            command = json.loads(request.content)["ins_api"]["input"]
            if command.startswith("show vlan"):
                return httpx.Response(200, json=wrap(VLAN_BODY))
            return httpx.Response(403)

        driver = nxos_driver(handler)
        vlans = await driver.get_vlans()
        await driver.close()
        assert [v.vlan_id for v in vlans] == [110]
        assert vlans[0].vxlan_vni is None

    async def test_facts_are_read(self) -> None:
        driver = nxos_driver(route({"show version": VERSION_BODY}))
        facts = await driver.get_facts()
        await driver.close()
        assert facts.model == "Nexus9000 C93180YC-EX chassis"
        assert facts.os_version == "9.3(10)"

    async def test_facts_failure_is_not_fatal(self) -> None:
        driver = nxos_driver(lambda request: httpx.Response(500))
        facts = await driver.get_facts()
        await driver.close()
        assert facts.model is None


class TestNxosErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failures_map_to_authentication_error(self, status: int) -> None:
        driver = nxos_driver(lambda request: httpx.Response(status))
        with pytest.raises(DriverAuthenticationError, match="role"):
            await driver.get_vlans()
        await driver.close()

    async def test_404_mentions_feature_nxapi(self) -> None:
        """The single most likely misconfiguration on a fresh Nexus."""
        driver = nxos_driver(lambda request: httpx.Response(404))
        with pytest.raises(DriverError, match="feature nxapi"):
            await driver.get_vlans()
        await driver.close()

    async def test_500_is_a_driver_error(self) -> None:
        driver = nxos_driver(lambda request: httpx.Response(500))
        with pytest.raises(DriverError):
            await driver.get_vlans()
        await driver.close()

    async def test_timeout_maps_to_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        driver = nxos_driver(handler)
        with pytest.raises(DriverConnectionError, match="in time"):
            await driver.get_vlans()
        await driver.close()

    async def test_transport_error_maps_to_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        driver = nxos_driver(handler)
        with pytest.raises(DriverConnectionError):
            await driver.get_vlans()
        await driver.close()

    async def test_non_json_body_maps_to_parse_error(self) -> None:
        driver = nxos_driver(lambda request: httpx.Response(200, text="<html>nope"))
        with pytest.raises(DriverParseError, match="non-JSON"):
            await driver.get_vlans()
        await driver.close()

    async def test_rejected_command_raises_rather_than_reporting_no_vlans(self) -> None:
        """Must not be mistaken for 'this switch has no VLANs'."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ins_api": {
                        "outputs": {"output": {"code": "400", "msg": "Invalid command", "body": ""}}
                    }
                },
            )

        driver = nxos_driver(handler)
        with pytest.raises(DriverParseError):
            await driver.get_vlans()
        await driver.close()

    async def test_every_failure_is_a_driver_error(self) -> None:
        """One except clause in the sync service must catch all of these, or an
        unreadable switch could reach the reconciler."""
        for handler in (
            lambda request: httpx.Response(401),
            lambda request: httpx.Response(404),
            lambda request: httpx.Response(500),
            lambda request: httpx.Response(200, text="not json"),
        ):
            driver = nxos_driver(handler)
            with pytest.raises(DriverError):
                await driver.get_vlans()
            await driver.close()


class TestIosXeDriver:
    def test_satisfies_the_driver_protocol(self) -> None:
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        assert isinstance(driver, NetworkDeviceDriver)

    async def test_missing_netmiko_gives_actionable_guidance(self) -> None:
        """netmiko is an optional extra. The failure must name the fix, and must be
        a DriverError so it is recorded as a switch failure rather than crashing
        the run."""
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        with pytest.raises(DriverDependencyError) as exc_info:
            await driver.connect()
        message = str(exc_info.value)
        assert "netmiko is not installed" in message
        assert "[cisco]" in message
        assert isinstance(exc_info.value, DriverError)

    async def test_reads_before_connect_are_refused(self) -> None:
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        with pytest.raises(DriverConnectionError, match="Not connected"):
            await driver.get_vlans()

    async def test_close_is_safe_when_never_connected(self) -> None:
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        await driver.close()

    def test_password_auth_kwargs(self) -> None:
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE, hostname="10.1.1.1"),
            PASSWORD_CREDENTIAL,
            DriverOptions(connect_timeout=11, command_timeout=22),
        )
        kwargs = driver._connect_kwargs()
        assert kwargs["device_type"] == "cisco_ios"
        assert kwargs["host"] == "10.1.1.1"
        assert kwargs["username"] == "nas-readonly"
        assert kwargs["password"] == "unused-in-tests"
        assert kwargs["conn_timeout"] == 11
        assert kwargs["timeout"] == 22
        assert kwargs["use_keys"] is False

    def test_agent_and_ssh_config_are_never_used(self) -> None:
        """The only credentials this process may use are the provisioned ones."""
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        assert driver._connect_kwargs()["allow_agent"] is False

    def test_ssh_key_auth_kwargs(self) -> None:
        driver = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), KEY_CREDENTIAL, DriverOptions()
        )
        kwargs = driver._connect_kwargs()
        assert kwargs["use_keys"] is True
        assert kwargs["key_file"] == "/etc/nas/keys/id_ed25519"
        assert "password" not in kwargs

    def test_enable_secret_is_only_sent_when_configured(self) -> None:
        without = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), PASSWORD_CREDENTIAL, DriverOptions()
        )
        assert "secret" not in without._connect_kwargs()

        with_enable = CiscoIosXeDriver(
            make_switch(vendor=Vendor.CISCO_IOSXE), ENABLE_CREDENTIAL, DriverOptions()
        )
        assert with_enable._connect_kwargs()["secret"] == "enable-secret"


class TestSecretHygiene:
    def test_credential_repr_hides_the_enable_secret(self) -> None:
        assert "enable-secret" not in repr(ENABLE_CREDENTIAL)
