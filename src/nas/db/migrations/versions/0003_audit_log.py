"""audit_log: an immutable record of security-relevant events

Milestone 4. Designed in Milestone 1 and deferred until there was something worth
attributing — `POST /sync`, which reaches production hardware.

Design points worth knowing before altering this schema:

* **No `updated_at`, and no trigger that would allow one.** Every other table uses
  the created/updated pair from `TimestampMixin`; this one deliberately does not. A
  row that can be modified after the fact is not an audit record. `occurred_at` is
  the only timestamp.
* **Attribution is stored twice.** `api_key_id` is a live foreign key for joins;
  `api_key_name` is a snapshot. The FK is `ON DELETE SET NULL`, so revoking and
  deleting a key leaves the history of what it did intact — the same reasoning as
  `sync_run_switches.switch_name`.
* **`actor` is caller-asserted, not verified.** NAS authenticates the API key, not
  the human behind it; ClientManager forwards the logged-in user. It is exactly as
  trustworthy as the calling application, which is why `api_key_name` sits beside it
  rather than being replaced by it.
* `action` and `outcome` are plain strings with no CHECK constraint, matching the
  `switches.vendor` precedent: adding an action needs an enum member, not a
  migration.
* Only `occurred_at` and `action` are indexed. "What happened recently" and
  "everything of this kind" are the queries a runbook actually runs; the rest is
  forensic and can afford a scan.

Nothing here is on the request hot path, so there is no unique constraint to
serialise concurrent writers.

Revision ID: 0003_audit_log
Revises: 0002_vlan_discovery
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_audit_log"
down_revision: str | None = "0002_vlan_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("api_key_name", sa.String(length=100), nullable=True),
        sa.Column("actor", sa.String(length=320), nullable=True),
        sa.Column("source_ip", sa.String(length=45), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_audit_log_api_key_id_api_keys"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_table("audit_log")
