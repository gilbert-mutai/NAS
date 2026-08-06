"""API key generation and verification.

Key format::

    nas_<prefix>_<secret>
         ^^^^^^   ^^^^^^^^
         8 hex    43 url-safe chars (256 bits of entropy)

The secret is base64url, so it may contain ``_`` and ``-``. Parsing therefore
splits on the first two underscores only — see ``extract_prefix``.

The plaintext key is shown exactly once, at creation. Only ``prefix`` and a
SHA-256 digest of the full key are persisted, so a database dump cannot be
replayed against the API.

Why SHA-256 rather than bcrypt/argon2: these keys are 256 bits of CSPRNG output,
not human-chosen passwords. There is no dictionary to attack and no feasible
brute-force, so a deliberately slow KDF buys no security here while adding
per-request latency to every single call. Slow KDFs exist to compensate for low
entropy; that problem does not apply.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import StrEnum

KEY_NAMESPACE = "nas"
PREFIX_BYTES = 4  # -> 8 hex characters
SECRET_BYTES = 32  # -> 256 bits of entropy


class Scope(StrEnum):
    """Capabilities an API key may carry. Least privilege by default."""

    VLANS_READ = "vlans:read"
    SWITCHES_READ = "switches:read"
    SYNC_WRITE = "sync:write"
    SYNC_READ = "sync:read"
    AUDIT_READ = "audit:read"
    """Read the audit trail. Deliberately outside READ_ONLY_SCOPES: the trail
    records who triggered what, and a key that only needs to look up VLANs has no
    business reading it. Grant it explicitly, to an operator's key."""

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


# Convenience bundle for ClientManager, which only ever reads and triggers syncs.
READ_ONLY_SCOPES: tuple[Scope, ...] = (
    Scope.VLANS_READ,
    Scope.SWITCHES_READ,
    Scope.SYNC_READ,
)


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """A freshly minted key. ``plaintext`` is never persisted or logged."""

    plaintext: str
    prefix: str
    key_hash: str


def generate_api_key() -> GeneratedApiKey:
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    plaintext = f"{KEY_NAMESPACE}_{prefix}_{secret}"
    return GeneratedApiKey(
        plaintext=plaintext,
        prefix=prefix,
        key_hash=hash_api_key(plaintext),
    )


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def extract_prefix(plaintext: str) -> str | None:
    """Return the lookup prefix from a presented key, or None if malformed.

    Parsing is strict: a key that does not match the expected shape is rejected
    without a database round-trip.
    """
    # maxsplit=2: the secret is base64url, whose alphabet includes '_', so the
    # secret may itself contain the separator. Only the first two underscores
    # delimit fields.
    parts = plaintext.split("_", 2)
    if len(parts) != 3:
        return None
    namespace, prefix, secret = parts
    if namespace != KEY_NAMESPACE or not secret:
        return None
    if len(prefix) != PREFIX_BYTES * 2:
        return None
    try:
        int(prefix, 16)
    except ValueError:
        return None
    return prefix


def verify_api_key(plaintext: str, expected_hash: str) -> bool:
    """Constant-time comparison of a presented key against a stored digest."""
    return hmac.compare_digest(hash_api_key(plaintext), expected_hash)


def redact(value: str, *, keep: int = 4) -> str:
    """Render a secret safe for logs: ``nas_1a2b…`` rather than the whole key."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'…'}"
