"""Authentication and authorisation logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nas.core.errors import (
    ErrorCode,
    InsufficientScopeError,
    InvalidApiKeyError,
    UnauthenticatedError,
)
from nas.core.security import Scope, generate_api_key
from nas.domain.entities import ApiKey
from nas.services.auth import AuthenticationService
from tests.fakes import InMemoryApiKeyRepository

NOW = datetime.now(UTC)


def build_key(
    *,
    scopes: frozenset[str] | None = None,
    is_active: bool = True,
    expires_at: datetime | None = None,
) -> tuple[str, ApiKey]:
    generated = generate_api_key()
    key = ApiKey(
        id=7,
        name="crm",
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=scopes if scopes is not None else frozenset({Scope.SWITCHES_READ.value}),
        is_active=is_active,
        created_at=NOW - timedelta(days=1),
        expires_at=expires_at,
    )
    return generated.plaintext, key


class TestAuthenticate:
    async def test_valid_key_is_accepted(self) -> None:
        plaintext, key = build_key()
        service = AuthenticationService(InMemoryApiKeyRepository([key]))
        assert (await service.authenticate(plaintext)).id == key.id

    async def test_usage_is_recorded(self) -> None:
        plaintext, key = build_key()
        repository = InMemoryApiKeyRepository([key])
        await AuthenticationService(repository).authenticate(plaintext)
        assert [entry[0] for entry in repository.marked_used] == [key.id]

    @pytest.mark.parametrize("presented", [None, ""])
    async def test_missing_key_is_unauthenticated(self, presented: str | None) -> None:
        service = AuthenticationService(InMemoryApiKeyRepository())
        with pytest.raises(UnauthenticatedError):
            await service.authenticate(presented)

    async def test_malformed_key_is_rejected(self) -> None:
        service = AuthenticationService(InMemoryApiKeyRepository())
        with pytest.raises(InvalidApiKeyError):
            await service.authenticate("garbage")

    async def test_unknown_prefix_is_rejected(self) -> None:
        plaintext, _ = build_key()
        service = AuthenticationService(InMemoryApiKeyRepository())
        with pytest.raises(InvalidApiKeyError):
            await service.authenticate(plaintext)

    async def test_correct_prefix_with_wrong_secret_is_rejected(self) -> None:
        """A stored prefix must not be enough — the digest has to match too."""
        _, key = build_key()
        forged = f"nas_{key.prefix}_wrong-secret-entirely"
        service = AuthenticationService(InMemoryApiKeyRepository([key]))
        with pytest.raises(InvalidApiKeyError):
            await service.authenticate(forged)

    async def test_revoked_key_is_rejected(self) -> None:
        plaintext, key = build_key(is_active=False)
        service = AuthenticationService(InMemoryApiKeyRepository([key]))
        with pytest.raises(InvalidApiKeyError):
            await service.authenticate(plaintext)

    async def test_expired_key_is_rejected(self) -> None:
        plaintext, key = build_key(expires_at=NOW - timedelta(minutes=1))
        service = AuthenticationService(InMemoryApiKeyRepository([key]))
        with pytest.raises(InvalidApiKeyError):
            await service.authenticate(plaintext)

    async def test_unusable_key_usage_is_not_recorded(self) -> None:
        plaintext, key = build_key(is_active=False)
        repository = InMemoryApiKeyRepository([key])
        with pytest.raises(InvalidApiKeyError):
            await AuthenticationService(repository).authenticate(plaintext)
        assert repository.marked_used == []

    @pytest.mark.parametrize(
        "presented",
        ["garbage", "nas_deadbeef_unknown-key-secret-value-here"],
    )
    async def test_rejections_are_indistinguishable(self, presented: str) -> None:
        """Malformed and unknown keys must produce the same code and message.

        Distinct errors would let a caller probe which prefixes exist.
        """
        _, key = build_key(is_active=False)
        service = AuthenticationService(InMemoryApiKeyRepository([key]))
        with pytest.raises(InvalidApiKeyError) as exc_info:
            await service.authenticate(presented)
        assert exc_info.value.code is ErrorCode.INVALID_API_KEY
        assert exc_info.value.message == InvalidApiKeyError.message


class TestAuthorize:
    def test_key_with_required_scope_passes(self) -> None:
        _, key = build_key(scopes=frozenset({Scope.SWITCHES_READ.value}))
        AuthenticationService.authorize(key, frozenset({Scope.SWITCHES_READ.value}))

    def test_no_required_scopes_passes(self) -> None:
        _, key = build_key(scopes=frozenset())
        AuthenticationService.authorize(key, frozenset())

    def test_missing_scope_is_rejected(self) -> None:
        _, key = build_key(scopes=frozenset({Scope.SWITCHES_READ.value}))
        with pytest.raises(InsufficientScopeError) as exc_info:
            AuthenticationService.authorize(key, frozenset({Scope.SYNC_WRITE.value}))
        assert exc_info.value.details["missing_scopes"] == [Scope.SYNC_WRITE.value]

    def test_partial_scope_coverage_is_rejected(self) -> None:
        _, key = build_key(scopes=frozenset({Scope.SWITCHES_READ.value}))
        required = frozenset({Scope.SWITCHES_READ.value, Scope.VLANS_READ.value})
        with pytest.raises(InsufficientScopeError) as exc_info:
            AuthenticationService.authorize(key, required)
        assert exc_info.value.details["missing_scopes"] == [Scope.VLANS_READ.value]
