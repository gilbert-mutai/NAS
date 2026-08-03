"""Settings parsing and boot-time hardening checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nas.core.config import Environment, Settings
from tests.conftest import DUMMY_DATABASE_URL, build_settings


class TestCsvParsing:
    def test_ip_ranges_accept_comma_separated_string(self) -> None:
        settings = build_settings(allowed_ip_ranges="10.0.0.1, 192.168.1.0/24")
        assert settings.allowed_ip_ranges == ["10.0.0.1", "192.168.1.0/24"]

    def test_ip_ranges_accept_a_list(self) -> None:
        settings = build_settings(allowed_ip_ranges=["10.0.0.1"])
        assert settings.allowed_ip_ranges == ["10.0.0.1"]

    def test_blank_entries_are_dropped(self) -> None:
        assert build_settings(allowed_ip_ranges="10.0.0.1,,  ,").allowed_ip_ranges == ["10.0.0.1"]

    def test_cors_origins_accept_comma_separated_string(self) -> None:
        settings = build_settings(cors_allow_origins="https://a.test,https://b.test")
        assert settings.cors_allow_origins == ["https://a.test", "https://b.test"]


class TestIpRangeValidation:
    @pytest.mark.parametrize("value", ["10.0.0.1", "10.0.0.0/8", "::1", "fd00::/8"])
    def test_accepts_valid_addresses_and_cidrs(self, value: str) -> None:
        assert build_settings(allowed_ip_ranges=[value]).allowed_ip_ranges == [value]

    @pytest.mark.parametrize("value", ["not-an-ip", "10.0.0.0/99", "300.1.1.1", "10.0.0.1-10"])
    def test_rejects_invalid_entries(self, value: str) -> None:
        with pytest.raises(ValidationError, match="not a valid IP address or CIDR"):
            build_settings(allowed_ip_ranges=[value])


class TestDeployedHardening:
    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_refuses_to_start_without_an_allowlist(self, environment: Environment) -> None:
        with pytest.raises(ValidationError, match="must be set in staging/production"):
            build_settings(environment=environment, allowed_ip_ranges=[])

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_starts_with_an_allowlist(self, environment: Environment) -> None:
        settings = build_settings(environment=environment, allowed_ip_ranges=["10.0.0.0/8"])
        assert settings.is_deployed is True

    @pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
    def test_open_allowlist_permitted_outside_deployment(self, environment: Environment) -> None:
        settings = build_settings(environment=environment, allowed_ip_ranges=[])
        assert settings.is_deployed is False


class TestRequiredValues:
    def test_database_url_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hermetic: must not depend on whether the developer has a .env file.

        `_env_file=None` disables dotenv loading and the env var is cleared, so this
        asserts the field's own requiredness rather than the ambient environment.
        """
        monkeypatch.delenv("NAS_DATABASE_URL", raising=False)
        with pytest.raises(ValidationError):
            Settings(environment=Environment.TEST, _env_file=None)  # type: ignore[call-arg]

    def test_settings_are_immutable(self) -> None:
        settings = build_settings()
        with pytest.raises(ValidationError):
            settings.log_level = "DEBUG"  # type: ignore[misc]

    def test_sqlalchemy_url_round_trips(self) -> None:
        assert build_settings().sqlalchemy_url == DUMMY_DATABASE_URL
