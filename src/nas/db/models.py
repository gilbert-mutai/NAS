"""Persistence models.

Milestone 1 covers device inventory (``switches``) and API authentication
(``api_keys``). VLAN tables arrive in Milestone 2.

Note what is *absent* from ``switches``: there is no password, key or secret
column of any kind. Only ``credential_ref`` — a name resolved outside the
database by a CredentialProvider. See nas/core/credentials.py.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from nas.db.base import Base, TimestampMixin
from nas.domain.enums import Vendor


class SwitchRow(Base, TimestampMixin):
    """A managed network device."""

    __tablename__ = "switches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("22"))

    # Stored as the enum's string value rather than a PostgreSQL ENUM type:
    # adding a vendor then needs no migration, and the application layer is the
    # single source of truth for which vendors exist.
    vendor: Mapped[str] = mapped_column(String(32), nullable=False)

    # A *name*, never a secret. Resolved by the credential provider at sync time.
    credential_ref: Mapped[str] = mapped_column(String(100), nullable=False)

    site: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # Reachability is tri-state: NULL means "never checked", which is different
    # from "checked and unreachable".
    is_reachable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("port > 0 AND port <= 65535", name="port_range"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(btrim(hostname)) > 0", name="hostname_not_blank"),
        Index("ix_switches_vendor", "vendor"),
        Index("ix_switches_is_active", "is_active"),
        Index("ix_switches_site", "site"),
    )

    def __repr__(self) -> str:
        return f"<SwitchRow id={self.id} name={self.name!r} vendor={self.vendor!r}>"


class ApiKeyRow(Base, TimestampMixin):
    """An API credential issued to a consumer such as the Django CRM.

    Only the lookup prefix and a SHA-256 digest are stored; the plaintext key is
    displayed once at creation and never recoverable.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexed and unique: authentication looks the key up by prefix, then does a
    # constant-time digest comparison.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("length(key_hash) = 64", name="key_hash_is_sha256"),
        Index("ix_api_keys_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        # key_hash is deliberately excluded.
        return f"<ApiKeyRow id={self.id} name={self.name!r} prefix={self.prefix!r}>"


__all__ = ["ApiKeyRow", "SwitchRow", "Vendor"]
