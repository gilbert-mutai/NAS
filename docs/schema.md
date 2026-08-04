# Database schema

PostgreSQL 16. Migration `0001_initial`.

NAS owns its own database with its own role. The CRM's database user has no access to it, and
NAS's user has no access to the CRM's — least privilege at the database level, not just the
application level.

## `switches` — device inventory

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | integer | no | serial | PK |
| `name` | varchar(100) | no | | Unique. Operator-facing identifier |
| `hostname` | varchar(255) | no | | IP or DNS name |
| `port` | integer | no | 22 | SSH/NETCONF port |
| `vendor` | varchar(32) | no | | `juniper`, `cisco`, `mikrotik`, `arista`, `hp`, `huawei`, `mock` |
| `credential_ref` | varchar(100) | no | | **A name, never a secret** |
| `site` | varchar(100) | yes | | POP or datacentre, e.g. `ADC NBO` |
| `environment` | varchar(50) | yes | | Free-text label |
| `model` | varchar(100) | yes | | Populated by discovery (Milestone 2) |
| `os_version` | varchar(100) | yes | | Populated by discovery (Milestone 2) |
| `description` | text | yes | | |
| `is_active` | boolean | no | true | Inactive switches are skipped by sync |
| `is_reachable` | boolean | **yes** | | Tri-state — see below |
| `last_health_check` | timestamptz | yes | | |
| `health_error` | text | yes | | |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` | |

**Constraints**

| Name | Rule |
|---|---|
| `pk_switches` | primary key on `id` |
| `uq_switches_name` | unique on `name` |
| `ck_switches_port_range` | `port > 0 AND port <= 65535` |
| `ck_switches_name_not_blank` | `length(btrim(name)) > 0` |
| `ck_switches_hostname_not_blank` | `length(btrim(hostname)) > 0` |

**Indexes:** `ix_switches_vendor`, `ix_switches_is_active`, `ix_switches_site`.

### Notable decisions

**There is no credential column.** No `password`, no `ssh_password`, no `private_key`. Only
`credential_ref`, a name resolved outside the database by a `CredentialProvider`. A database
dump therefore grants no access to any network device. This is a deliberate departure from
the CRM's existing `pbx_backups.CXFTPServer`, which stores `ssh_password` as plaintext.

**`is_reachable` is nullable on purpose.** `NULL` means "never checked", which is a different
operational state from `FALSE` ("checked, and it was down"). Collapsing the two would make a
newly-registered switch indistinguishable from a failed one. The API surfaces this as
`reachability: unknown | reachable | unreachable`.

**`vendor` is `varchar`, not a PostgreSQL `ENUM`.** Adding a vendor then needs no migration,
and the application is the single source of truth for the vendor list. The cost is that the
database does not reject an unknown vendor string; the trade is worth it because vendor
support is expected to grow, and every write path goes through the validated `Vendor` enum.

**Check constraints duplicate application validation.** Defence in depth: a `psql` session, a
future service, or a bug in a repository still cannot write a blank hostname.

## `api_keys` — API authentication

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | integer | no | serial | PK |
| `name` | varchar(100) | no | | Unique. e.g. `crm` |
| `description` | text | yes | | |
| `prefix` | varchar(16) | no | | Unique. 8 hex chars, the lookup handle |
| `key_hash` | varchar(64) | no | | SHA-256 hex digest of the full key |
| `scopes` | text[] | no | `'{}'` | e.g. `{switches:read,vlans:read}` |
| `is_active` | boolean | no | true | Revocation flag |
| `expires_at` | timestamptz | yes | | `NULL` = no expiry |
| `last_used_at` | timestamptz | yes | | Best-effort; see below |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` | |

**Constraints:** `pk_api_keys`, `uq_api_keys_name`, `uq_api_keys_prefix`,
`ck_api_keys_key_hash_is_sha256` (`length(key_hash) = 64`). **Index:** `ix_api_keys_is_active`.

### Notable decisions

**The plaintext key is never stored.** Authentication parses the prefix from the presented
key, looks up the row by `prefix` (indexed, so one lookup), then does a constant-time digest
comparison. A stolen database yields no usable key.

**`last_used_at` is a convenience column, not an audit record.** It is written in the request
transaction, so a request that ends in an error rolls the timestamp back with everything else.
The structured access log is the authoritative record of key usage. Milestone 4 adds a proper
`audit_log` table.

## Migration policy

- Every schema change is an Alembic migration. `metadata.create_all()` is never used outside
  of throwaway experiments.
- Constraints get explicit, convention-derived names (`NAMING_CONVENTION` in `db/base.py`) so
  a later migration can drop them by name. Unnamed constraints are effectively permanent.
- Note the gotcha this repo already hit: the `ck` convention wraps an explicitly-named
  `CheckConstraint`, so a migration must pass the **bare** name (`port_range`), not the
  expanded one (`ck_switches_port_range`), or you get `ck_switches_ck_switches_port_range`.
- Every migration has a working `downgrade()`. The integration test suite runs
  `downgrade base` → `upgrade head` on every session, so a broken downgrade fails CI.
- Drift between the ORM and the migrations is checkable:

```bash
# Should report no differences
python - <<'PY'
import asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine
from nas.core.config import get_settings
from nas.db.base import Base
from nas.db import models  # noqa: F401

async def main():
    engine = create_async_engine(get_settings().sqlalchemy_url)
    async with engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(
            MigrationContext.configure(c, opts={"compare_type": True,
                                                "compare_server_default": True}),
            Base.metadata))
    await engine.dispose()
    print(diff or "no drift")

asyncio.run(main())
PY
```

## Milestone 2 additions (planned)

| Table | Purpose |
|---|---|
| `vlans` | Discovered VLANs. Unique on `(switch_id, vlan_id)` — the same VLAN legitimately exists on several switches. `state` moves `active → missing` rather than being deleted, preserving `first_seen_at`/`last_seen_at`. |
| `vlan_interfaces` | Port membership (interface name, access/trunk mode) — needed to answer "where is this VLAN in use". |
| `sync_runs` | One row per run: trigger, status (`running`/`success`/`partial`/`failed`), timings, counters. |
| `sync_run_switches` | Per-switch status and counters within a run, so a partial failure is attributable to a device. |

**Availability will be derived, never stored.** A VLAN is "available" when no row exists for it
in the queried scope. An `is_available` column would drift out of sync with the switches, which
defeats the point of the service.

---

## Milestone 2 — VLAN discovery

Migration `0002_vlan_discovery`.

### `vlans`

One row per VLAN **per switch**.

| Column | Notes |
|---|---|
| `id` | Surrogate key. Referenced by `vlan_interfaces.vlan_record_id` |
| `switch_id` | FK → `switches.id`, `ON DELETE CASCADE` |
| `vlan_id` | The **802.1Q tag**. `CHECK (vlan_id BETWEEN 1 AND 4094)` |
| `name`, `description`, `l3_interface`, `vxlan_vni` | As reported by the device |
| `state` | `active` \| `missing` |
| `first_seen_at` | Never overwritten — survives a disappear/reappear cycle |
| `last_seen_at` | Advances **only** when the VLAN is actually observed |
| `last_synced_at` | Advances whenever the switch was polled successfully |
| `raw` | JSONB of the driver payload, for audit and parser debugging |

`UNIQUE (switch_id, vlan_id)` · indexes on `(vlan_id, state)`, `(switch_id, state)`, `state`, `name`

Three decisions worth not reversing:

- **Uniqueness is `(switch_id, vlan_id)`, not `vlan_id`.** The same tag legitimately exists on
  many switches; answering "who is using 1234" means aggregating rows.
- **There is no `is_available` column.** Availability is derived at query time from the absence
  of an active row. A stored flag would drift from the switches — exactly the failure this
  service exists to prevent.
- **`last_seen_at` and `last_synced_at` are different things.** A VLAN marked missing has a
  `last_synced_at` newer than its `last_seen_at`: the switch answered, but no longer reports the
  VLAN. Conflating them makes staleness reporting meaningless.

### `vlan_interfaces`

Port membership — the "where is it used" detail.

| Column | Notes |
|---|---|
| `vlan_record_id` | FK → `vlans.id`, `ON DELETE CASCADE`. Named to avoid confusion with the 802.1Q tag |
| `name` | Interface name as reported, e.g. `ge-0/0/12.0` |
| `mode` | `access` \| `trunk` \| `unknown` |

`UNIQUE (vlan_record_id, name)`. Membership is replaced wholesale on change rather than diffed —
it is small and the driver always reports it in full.

### `sync_runs`

One row per synchronisation pass: `trigger` (`scheduled`/`manual`/`cli`), `status`
(`running`/`success`/`partial`/`failed`), timings, `correlation_id`, and aggregate counters
(`switches_*`, `vlans_*`).

`partial` is a first-class status. A run where some switches succeeded and others failed is
neither a success nor a failure, and the distinction is what tells an operator whether the data
is trustworthy.

### `sync_run_switches`

Per-switch outcome within a run, so a partial run is explainable rather than merely labelled.

`switch_id` is `ON DELETE SET NULL` while `switch_name` is a snapshot — run history stays
auditable after a switch is removed from inventory.

### Enum-backed columns

`state`, `mode`, `trigger`, `status` and `outcome` are plain `VARCHAR` with **no** CHECK
constraint, matching the `switches.vendor` precedent: the application owns the allowed values, so
adding one needs no migration. `vlan_id`'s range check is different — that is real data integrity,
not an enum.
