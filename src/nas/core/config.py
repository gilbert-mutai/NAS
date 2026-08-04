"""Application configuration.

All settings come from environment variables (prefix ``NAS_``) or a local ``.env``
file. Nothing security-sensitive has a usable production default: the database URL
must be supplied explicitly, and the IP allowlist defaults to "closed" outside of
local development.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# pydantic-settings JSON-decodes complex types (list, dict) straight from the
# environment, which makes `NAS_ALLOWED_IP_RANGES=10.0.0.0/8` — and even an empty
# value — a parse error before any validator runs. NoDecode hands the raw string
# to our own `_split_csv` validator instead, so these read as plain CSV.
type CsvList = Annotated[list[str], NoDecode]


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Application ───────────────────────────────────────────────────────────
    environment: Environment = Environment.LOCAL
    service_name: str = "nas"
    api_v1_prefix: str = "/api/v1"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ── Database ──────────────────────────────────────────────────────────────
    # Required. Must use the asyncpg driver, e.g.
    #   postgresql+asyncpg://nas:secret@localhost:5432/nas_dev
    database_url: PostgresDsn
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 5
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    db_pool_timeout: Annotated[int, Field(ge=1, le=300)] = 30
    db_echo: bool = False

    # ── Network access control ────────────────────────────────────────────────
    # CIDR blocks permitted to reach the API. An empty list means "allow any",
    # which is only tolerated in local/test environments (enforced below).
    allowed_ip_ranges: CsvList = Field(default_factory=list)

    # Only enable when a reverse proxy you control (Nginx) sets X-Forwarded-For.
    # When False, the client IP is taken from the socket and headers are ignored,
    # so a caller cannot spoof its way past the allowlist.
    trust_proxy_headers: bool = False

    # ── API surface ───────────────────────────────────────────────────────────
    docs_enabled: bool = True
    cors_allow_origins: CsvList = Field(default_factory=list)

    # ── Synchronisation ───────────────────────────────────────────────────────
    sync_enabled: bool = True
    """Whether the embedded scheduler runs periodic syncs."""

    sync_interval_seconds: Annotated[int, Field(ge=60, le=86_400)] = 900

    sync_max_concurrency: Annotated[int, Field(ge=1, le=32)] = 4
    """Switches polled in parallel. Device I/O dominates a run's wall-clock."""

    sync_allow_empty_discovery: bool = False
    """Permit a switch reporting zero VLANs to mark all its records missing.

    Off by default: an empty result is far more often a silent read failure than
    a genuine mass deletion. See nas.sync.reconciler.
    """

    sync_stale_run_minutes: Annotated[int, Field(ge=5, le=1440)] = 60
    """After this long, a still-'running' run is assumed abandoned and failed."""

    driver_connect_timeout: Annotated[int, Field(ge=1, le=300)] = 30
    driver_command_timeout: Annotated[int, Field(ge=1, le=600)] = 60

    driver_verify_tls: bool = False
    """Verify device TLS certificates (Cisco NX-API).

    Defaults False: Nexus switches ship self-signed certificates and NAS reaches
    them over a private management network, so verification would fail on
    essentially every device. Enable once the estate presents certificates NAS can
    validate. See nas.drivers.options.DriverOptions."""

    # ── Device credentials ────────────────────────────────────────────────────
    # Path to the 0600-mode YAML file holding switch credentials. Never stored in
    # the database. See credentials.example.yaml.
    credentials_file: Path | None = None

    # ─────────────────────────────────────────────────────────────────────────
    @field_validator("allowed_ip_ranges", "cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept comma-separated strings so these can be set from a single env var."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_ip_ranges")
    @classmethod
    def _validate_cidrs(cls, value: list[str]) -> list[str]:
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(f"{entry!r} is not a valid IP address or CIDR block") from exc
        return value

    @model_validator(mode="after")
    def _enforce_deployed_hardening(self) -> Settings:
        """Refuse to start a deployed instance with development-only settings.

        Failing at boot is deliberate: an open allowlist in staging or production
        would silently expose the only component that can reach the switches.
        """
        if (
            self.environment in (Environment.STAGING, Environment.PRODUCTION)
            and not self.allowed_ip_ranges
        ):
            raise ValueError(
                "NAS_ALLOWED_IP_RANGES must be set in staging/production. "
                "Refusing to start with an open IP allowlist."
            )
        return self

    @property
    def is_deployed(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

    @property
    def sqlalchemy_url(self) -> str:
        return str(self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so configuration is parsed and validated exactly once. Tests clear the
    cache via ``get_settings.cache_clear()``.
    """
    # Every field is populated from the environment or .env, so no arguments are
    # passed here; pydantic-settings validates and raises on anything missing.
    return Settings()
