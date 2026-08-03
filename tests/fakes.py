"""In-memory test doubles.

These implement the repository Protocols, which is what lets the entire API
surface be tested without PostgreSQL. Tests that must exercise real SQL live in
tests/integration and run against a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nas.core.credentials import AuthMethod, CredentialNotFoundError, DeviceCredential
from nas.domain.entities import ApiKey, Switch
from nas.domain.enums import Vendor
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import NewApiKey, NewSwitch, SwitchFilters

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
