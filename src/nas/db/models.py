"""Persistence models.

Milestone 1 covers device inventory (``switches``) and API authentication
(``api_keys``). VLAN tables arrive in Milestone 2.

Note what is *absent* from ``switches``: there is no password, key or secret
column of any kind. Only ``credential_ref`` — a name resolved outside the
database by a CredentialProvider. See nas/core/credentials.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    """An API credential issued to a consumer such as ClientManager.

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


class VlanRow(Base, TimestampMixin):
    """A VLAN discovered on one switch.

    Uniqueness is ``(switch_id, vlan_id)``. The same 802.1Q tag legitimately
    exists on many switches, so the tag alone cannot be the key — and answering
    "who is using VLAN 1234" means aggregating rows across switches.

    Note the column naming: ``vlan_id`` is the **802.1Q tag** (matching the API),
    while ``id`` is the surrogate primary key. ``vlan_interfaces`` therefore
    references ``vlan_record_id`` rather than ``vlan_id``, to keep the two
    unambiguous.
    """

    __tablename__ = "vlans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    switch_id: Mapped[int] = mapped_column(
        ForeignKey("switches.id", ondelete="CASCADE"), nullable=False
    )

    # The 802.1Q tag.
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    l3_interface: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vxlan_vni: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # active | missing. Removal is soft — see VlanState.
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Exactly what the driver reported, for audit and for diagnosing parser
    # changes against real device output. Never interpreted by the core.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    interfaces: Mapped[list[VlanInterfaceRow]] = relationship(
        back_populates="vlan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VlanInterfaceRow.name",
    )

    __table_args__ = (
        UniqueConstraint("switch_id", "vlan_id", name="uq_vlans_switch_id_vlan_id"),
        CheckConstraint("vlan_id >= 1 AND vlan_id <= 4094", name="vlan_id_range"),
        # Serves the cross-switch lookup that answers "is this tag free?".
        Index("ix_vlans_vlan_id_state", "vlan_id", "state"),
        Index("ix_vlans_switch_id_state", "switch_id", "state"),
        Index("ix_vlans_state", "state"),
        Index("ix_vlans_name", "name"),
    )

    def __repr__(self) -> str:
        return f"<VlanRow id={self.id} switch={self.switch_id} vlan_id={self.vlan_id}>"


class VlanInterfaceRow(Base, TimestampMixin):
    """An interface carrying a VLAN — the "where is it used" detail."""

    __tablename__ = "vlan_interfaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # References the vlans row, not an 802.1Q tag.
    vlan_record_id: Mapped[int] = mapped_column(
        ForeignKey("vlans.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # access | trunk | unknown
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'unknown'"))

    vlan: Mapped[VlanRow] = relationship(back_populates="interfaces")

    __table_args__ = (
        UniqueConstraint("vlan_record_id", "name", name="uq_vlan_interfaces_vlan_record_id_name"),
        Index("ix_vlan_interfaces_vlan_record_id", "vlan_record_id"),
        Index("ix_vlan_interfaces_name", "name"),
    )

    def __repr__(self) -> str:
        return f"<VlanInterfaceRow id={self.id} name={self.name!r} mode={self.mode!r}>"


class SyncRunRow(Base, TimestampMixin):
    """One synchronisation pass."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ties this run to the API request that triggered it, and to every log line
    # the run emitted.
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    switches_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    switches_succeeded: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    switches_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    switches_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_discovered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_marked_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    switch_results: Mapped[list[SyncRunSwitchRow]] = relationship(
        back_populates="sync_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SyncRunSwitchRow.switch_name",
    )

    __table_args__ = (
        # "Latest run" and the run-history list are the two hot queries. A plain
        # ascending index serves ORDER BY ... DESC fine (PostgreSQL scans B-trees
        # backwards), and avoids the perpetual autogenerate drift that an
        # expression index on started_at DESC produces.
        Index("ix_sync_runs_started_at", "started_at"),
        Index("ix_sync_runs_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<SyncRunRow id={self.id} status={self.status!r} trigger={self.trigger!r}>"


class SyncRunSwitchRow(Base, TimestampMixin):
    """Per-switch outcome within a run.

    ``switch_name`` is a snapshot, and ``switch_id`` becomes NULL if the switch is
    later deleted — run history has to survive inventory changes to stay
    auditable.
    """

    __tablename__ = "sync_run_switches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    sync_run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    switch_id: Mapped[int | None] = mapped_column(
        ForeignKey("switches.id", ondelete="SET NULL"), nullable=True
    )
    switch_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # success | failed | skipped
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    vlans_discovered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    vlans_marked_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    sync_run: Mapped[SyncRunRow] = relationship(back_populates="switch_results")

    __table_args__ = (Index("ix_sync_run_switches_sync_run_id", "sync_run_id"),)

    def __repr__(self) -> str:
        return f"<SyncRunSwitchRow run={self.sync_run_id} switch={self.switch_name!r}>"


class AuditLogRow(Base):
    """An immutable record of a security-relevant event.

    Deliberately **not** a ``TimestampMixin`` user. The mixin brings ``updated_at``
    and an ``onupdate`` hook, and a row that can be updated is not an audit record.
    ``occurred_at`` is the only time this table needs.

    Attribution is stored twice on purpose. ``api_key_id`` gives a live foreign key
    for joins, and ``api_key_name`` is a snapshot that survives the key being
    revoked and deleted — the same reasoning as ``sync_run_switches.switch_name``.
    Deleting a key must not erase the history of what it did.

    ``actor`` is the human identity forwarded by the caller (ClientManager sends the
    logged-in user). It is **caller-asserted, not verified** — NAS authenticates the
    API key, not the person behind it. Trustworthy exactly as far as the calling
    application is, which is why the key name is kept alongside it rather than
    replaced by it.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # AuditAction / AuditOutcome. Plain strings with no CHECK constraint, matching
    # switches.vendor: adding an action needs an enum member, not a migration.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    api_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 320 = the maximum length of an email address (64 local + @ + 255 domain).
    actor: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # 45 covers an IPv4-mapped IPv6 address, the longest textual form.
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Ties this entry to the request's log lines and, for a sync, to sync_runs.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # What was acted on, e.g. ("sync_run", "42"). target_id is text so it can hold
    # a name as readily as an integer id.
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Action-specific context. Never secrets — see AuditService for what is allowed.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # "What happened recently" is the only hot query; the rest is forensic and
        # can scan. Ascending order serves ORDER BY ... DESC fine.
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_action", "action"),
    )

    def __repr__(self) -> str:
        return f"<AuditLogRow id={self.id} action={self.action!r} outcome={self.outcome!r}>"


__all__ = [
    "ApiKeyRow",
    "AuditLogRow",
    "SwitchRow",
    "SyncRunRow",
    "SyncRunSwitchRow",
    "Vendor",
    "VlanInterfaceRow",
    "VlanRow",
]
