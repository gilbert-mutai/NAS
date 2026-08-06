# API reference — v1

Base path `/api/v1`. Interactive docs at `/docs`, machine-readable at `/openapi.json`.

Versioning is by URL prefix so a future `v2` can run alongside `v1` while consumers migrate.

## Authentication

Every `/api/v1` endpoint requires an `X-API-Key` header.

```bash
curl -H "X-API-Key: nas_1a2b3c4d_EXAMPLE-KEY-NOT-A-REAL-SECRET" \
     https://nas.internal/api/v1/switches
```

Keys are issued by an operator with shell access:

```bash
nas apikey create --name clientmanager --scopes switches:read,vlans:read,sync:read
```

The key is displayed once and is not recoverable — only a SHA-256 digest is stored.

### Scopes

A route declares the scopes it needs; a key lacking any of them is rejected with `403`.

| Scope | Grants |
|---|---|
| `switches:read` | Read the switch inventory |
| `vlans:read` | Read VLAN data (Milestone 2) |
| `sync:read` | Read synchronisation status and history (Milestone 2) |
| `sync:write` | Trigger a synchronisation (Milestone 2) |

Grant the minimum. ClientManager needs the three read scopes plus, if you want a "Sync Now" button,
`sync:write` — nothing else.

### Rejection semantics

| Situation | Status | Code |
|---|---|---|
| Source IP outside the allowlist | 403 | `IP_NOT_ALLOWED` |
| No `X-API-Key` header | 401 | `UNAUTHENTICATED` |
| Malformed, unknown, revoked or expired key | 401 | `INVALID_API_KEY` |
| Valid key, missing scope | 403 | `INSUFFICIENT_SCOPE` |

The four *invalid key* cases deliberately return one identical code and message. Distinguishing
them would let a caller probe which key prefixes exist. The IP check runs before authentication,
so a disallowed address cannot use the API to test key validity at all.

## Response conventions

**Collections** — `data` plus `pagination`:

```json
{
  "data": [ { "id": 1, "name": "adc-core-sw1" } ],
  "pagination": {
    "page": 1, "page_size": 50, "total": 2, "total_pages": 1,
    "has_next": false, "has_previous": true
  }
}
```

**Single resources** — the object itself, unwrapped.

**Errors** — always the same envelope, from every endpoint and every failure mode:

```json
{
  "error": {
    "code": "SWITCH_NOT_FOUND",
    "message": "No switch exists with id 999.",
    "request_id": "8b103cc7f30d4771860f9b3bc63ae1ff",
    "details": { "switch_id": 999 }
  }
}
```

`details` is optional and structured for programmatic use. `request_id` matches the
`X-Request-ID` response header and appears in NAS's logs — quote it in a bug report.

### Request correlation

Send `X-Request-ID` and NAS will adopt and echo it, so a ClientManager request traces across both
services. Omit it and NAS generates one. Inbound values are length-capped at 64 characters and
stripped to `[A-Za-z0-9._-]`, because they land in log records.

### Error codes

| Code | Status | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Query or body failed validation; `details.fields` lists locations |
| `UNAUTHENTICATED` | 401 | No API key supplied |
| `INVALID_API_KEY` | 401 | Key is malformed, unknown, revoked or expired |
| `INSUFFICIENT_SCOPE` | 403 | Key lacks a required scope |
| `IP_NOT_ALLOWED` | 403 | Source address outside the allowlist |
| `NOT_FOUND` | 404 | No such route or resource |
| `SWITCH_NOT_FOUND` | 404 | No switch with that id |
| `CONFLICT` | 409 | Uniqueness violation |
| `INTERNAL_ERROR` | 500 | Unexpected failure. Details are logged, never returned |
| `CONFIGURATION_ERROR` | 500 | Service misconfiguration |

Validation errors report each field's `location`, `message` and `type` but **never the
submitted value** — a malformed secret in a request is not echoed back or logged.

---

## Health endpoints

Unauthenticated, and exempt from the IP allowlist so probes need no whitelisting. They expose
no switch, customer or credential data.

### `GET /live`

Liveness. Touches no dependency. A failure means restart the process.

```json
{"status": "alive", "service": "nas", "version": "0.1.0"}
```

### `GET /ready`

Readiness. Checks the database. `200` when ready, `503` when not — de-pool the instance, do
not restart it.

```json
{"status": "ready", "database": "ok"}
```

### `GET /health`

Summary for humans and monitoring. `503` when degraded.

```json
{"status": "ok", "service": "nas", "version": "0.1.0",
 "environment": "local", "database": "ok"}
```

---

## `GET /api/v1/switches`

Lists managed devices. Requires `switches:read`.

| Query parameter | Type | Notes |
|---|---|---|
| `vendor` | enum | `juniper`, `cisco`, `mikrotik`, `arista`, `hp`, `huawei`, `mock` |
| `site` | string | Exact match, case-insensitive. Max 100 chars |
| `environment` | string | Exact match, case-insensitive. Max 50 chars |
| `is_active` | bool | |
| `q` | string | Free-text over name, hostname, description. Max 200 chars |
| `page` | int ≥ 1 | Default 1 |
| `page_size` | int 1–500 | Default 50 |

Results are ordered by `name`. `%` and `_` in `q` are escaped and match literally.

```bash
curl -H "X-API-Key: $NAS_API_KEY" \
  "https://nas.internal/api/v1/switches?vendor=juniper&site=ADC%20NBO&page_size=20"
```

### Response fields

| Field | Notes |
|---|---|
| `id`, `name`, `hostname`, `port` | |
| `vendor`, `vendor_label` | Machine value and display label |
| `credential_ref` | The credential **name**. Never a secret |
| `credential_status` | `resolved` \| `missing` \| `not_configured` |
| `site`, `environment`, `description` | |
| `model`, `os_version` | `null` until discovery populates them (Milestone 2) |
| `is_active` | Inactive switches are skipped by sync |
| `reachability` | `unknown` \| `reachable` \| `unreachable` |
| `last_health_check`, `health_error` | |
| `created_at`, `updated_at` | ISO 8601, UTC |

**On `credential_status`.** This is the one place the API says anything about credentials, and
it says only whether the reference resolves — never the username, password or key path.
`missing` means the store is configured but has no entry with that name; `not_configured` means
no credential store is configured at all. The two have different fixes, so they are reported
separately. Anything other than `resolved` means synchronisation will fail for that device.

**On `reachability`.** `unknown` is a real state, distinct from `unreachable`: it means the
device has never been contacted, not that it is down.

## `GET /api/v1/switches/{switch_id}`

One switch, unwrapped. Requires `switches:read`. Returns `404 SWITCH_NOT_FOUND` if absent,
`422` if `switch_id` is not a positive integer.

---

## Planned in Milestone 2

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/vlans` | Search and filter discovered VLANs |
| `GET /api/v1/vlans/{id}` | One VLAN record with port membership |
| `GET /api/v1/vlans/lookup/{vlan_id}` | Aggregate a VLAN id across all switches **and return an availability verdict** — the single call that answers "is 1234 free, and if not, who has it and where" |
| `POST /api/v1/sync` | Trigger synchronisation. `202 Accepted` with a run id. Requires `sync:write` |
| `GET /api/v1/sync/status` | Latest run |
| `GET /api/v1/sync/runs`, `/runs/{id}` | Run history and per-switch detail |

## Client notes for ClientManager

- Call server-to-server. There is no CORS configuration by default and none is needed.
- Set a timeout on every call and degrade to a visible "NAS unreachable" banner rather than a
  500 — ClientManager should stay usable when NAS is down.
- Forward a correlation id as `X-Request-ID` so a user-reported problem can be traced across
  both services' logs.
- Read `pagination.has_next` rather than inferring the end of a collection from a short page.
- Treat `credential_status != "resolved"` as an operator-facing warning in the UI. It is a
  configuration problem someone needs to fix, not a transient error.

---

# Milestone 2 endpoints

## `GET /api/v1/vlans`

Search discovered VLANs. Requires `vlans:read`. One row per VLAN **per switch**.

| Query parameter | Notes |
|---|---|
| `vlan_id` | 802.1Q tag, 1–4094 |
| `switch_id` | |
| `site` | Exact, case-insensitive |
| `state` | `active` \| `missing` |
| `q` | Free text over VLAN name, description **and member interface name** |
| `page`, `page_size` | 1-indexed; page_size 1–500, default 50 |

Ordered by `vlan_id`, then switch name. `%` and `_` in `q` are escaped and match literally.

Note `id` (record id) and `vlan_id` (802.1Q tag) are different fields. `last_seen_at` vs
`last_synced_at`: the latter being newer means the switch was reachable but no longer reports
this VLAN.

## `GET /api/v1/vlans/lookup/{vlan_id}`

**The endpoint that replaces an SSH session.** Aggregates one tag across every switch and returns
an availability verdict in a single call. Requires `vlans:read`.

```bash
curl -H "X-API-Key: $NAS_API_KEY" https://nas.internal/api/v1/vlans/lookup/1234
```

```json
{
  "vlan_id": 1234,
  "availability": "in_use",
  "is_available": false,
  "switch_count": 2,
  "active_usages": [ { "switch_name": "adc-core-sw1", "switch_site": "ADC NBO", "vlan": {} } ],
  "historic_usages": [],
  "data_as_of": "2026-08-04T08:19:46Z",
  "is_stale": false
}
```

| `availability` | Meaning |
|---|---|
| `available` | No active record on any switch in the inventory |
| `in_use` | Active on at least one switch — see `active_usages` |
| `reserved` | Tag 0 or 4095; reserved by 802.1Q and never assignable |

**Read `is_stale` before trusting `available`.** The switches remain authoritative; NAS holds a
synchronised cache. `is_stale` is true when the newest data is older than the staleness threshold,
and when nothing has ever been synced. `historic_usages` lists records now `missing` — useful
context before reusing a tag.

Tags outside 0–4095 are rejected with `422`.

## `POST /api/v1/sync`

Trigger a synchronisation. Requires `sync:write`. Returns `202` with the completed run.

Optional body: `{"switch_ids": [1, 2]}` to restrict the run.

Runs are serialised by a PostgreSQL advisory lock: a second concurrent request gets **409
CONFLICT** rather than queueing, so a double-clicked "Sync Now" cannot start two runs.

Two behaviours ClientManager should surface:

- A run where some switches fail completes with status `partial`. Read `switch_results` for
  per-switch attribution rather than treating the run as wholly good or bad.
- **A switch that cannot be read never has its VLANs marked missing.** Its data is left exactly
  as the last successful run left it, and the switch is reported `failed`.

## `GET /api/v1/sync/status`

The endpoint to poll for a dashboard. Requires `sync:read`.

```json
{"is_running": false, "never_run": false, "latest_run": { "id": 8, "status": "partial" }}
```

## `GET /api/v1/sync/runs` · `GET /api/v1/sync/runs/{id}`

History, newest first, and one run with per-switch detail. Requires `sync:read`. The list view
omits `switch_results`; fetch a single run for that.
