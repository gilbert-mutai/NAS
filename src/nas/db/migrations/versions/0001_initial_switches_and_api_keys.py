"""Initial schema: switches and api_keys

Milestone 1. Device inventory plus API authentication.

Deliberately absent from `switches`: any password, key or secret column. The
table stores only `credential_ref`, resolved outside the database by a
CredentialProvider, so a database dump grants no access to network devices.

Revision ID: 0001_initial
Revises: None
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "switches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), server_default=sa.text("22"), nullable=False),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("credential_ref", sa.String(length=100), nullable=False),
        sa.Column("site", sa.String(length=100), nullable=True),
        sa.Column("environment", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("os_version", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_reachable", sa.Boolean(), nullable=True),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("port > 0 AND port <= 65535", name="port_range"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("length(btrim(hostname)) > 0", name="hostname_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_switches"),
        sa.UniqueConstraint("name", name="uq_switches_name"),
    )
    op.create_index("ix_switches_vendor", "switches", ["vendor"], unique=False)
    op.create_index("ix_switches_is_active", "switches", ["is_active"], unique=False)
    op.create_index("ix_switches_site", "switches", ["site"], unique=False)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(key_hash) = 64", name="key_hash_is_sha256"),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("name", name="uq_api_keys_name"),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
    )
    op.create_index("ix_api_keys_is_active", "api_keys", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_is_active", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_switches_site", table_name="switches")
    op.drop_index("ix_switches_is_active", table_name="switches")
    op.drop_index("ix_switches_vendor", table_name="switches")
    op.drop_table("switches")
