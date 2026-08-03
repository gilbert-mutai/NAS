"""Domain entities.

Immutable, framework-free representations of the concepts this service manages.
Repositories translate persistence rows into these; services and the API layer
work with these rather than ORM objects, which keeps business rules testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from nas.domain.enums import CredentialStatus, ReachabilityState, Vendor


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
    """A credential issued to an API consumer, e.g. the Django CRM."""

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
