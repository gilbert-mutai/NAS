"""Repository interfaces.

Services depend on these Protocols, never on SQLAlchemy. Two payoffs:

1. API-level tests substitute in-memory fakes and need no database at all.
2. The persistence layer can change without touching business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nas.domain.entities import ApiKey, Switch
from nas.domain.enums import Vendor
from nas.domain.pagination import Page, PageRequest


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


class ApiKeyRepository(Protocol):
    async def get_by_prefix(self, prefix: str) -> ApiKey | None: ...

    async def list_all(self) -> tuple[ApiKey, ...]: ...

    async def create(self, data: NewApiKey) -> ApiKey: ...

    async def mark_used(self, api_key_id: int, *, when: datetime) -> None: ...

    async def revoke(self, name: str) -> bool: ...
