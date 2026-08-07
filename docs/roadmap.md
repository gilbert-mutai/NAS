# Roadmap

Phase 1 is VLAN **discovery** only. Delivered incrementally, each milestone ending in a
deployable state.

## Milestone 1 — Foundation and security spine ✅

Delivered and verified.

- Repository, packaging, ruff + `mypy --strict`, CI pipeline
- Configuration with boot-time hardening checks
- Structured JSON logging with request correlation
- Single error envelope and centralised handlers
- API key authentication (hashed, scoped, revocable, expiring)
- IP allowlist with explicit proxy-header trust
- Security headers
- `/health`, `/live`, `/ready`
- PostgreSQL + SQLAlchemy 2.0 + Alembic; `switches` and `api_keys`
- `CredentialProvider` Protocol + file-backed implementation
- `GET /api/v1/switches`, `GET /api/v1/switches/{id}`
- Operator CLI: `apikey`, `switch`, `credentials check`, `db`, `serve`
- Docker image (non-root, multi-stage) and Compose stack
- 208 tests; full documentation set

**Verified end-to-end:** migrations against live PostgreSQL with no ORM drift; every CLI command
including failure exit codes; live server probes and API calls; revocation taking effect;
under-scoped key rejected; staging boot refused without an allowlist; no secrets in logs;
non-root container with no baked-in secrets; containerised stack serving requests.

## Milestone 2 — COMPLETE (2026-08-04)

Delivered: driver abstraction (Juniper PyEZ + mock + registry), pure XML parser, `vlans` /
`vlan_interfaces` / `sync_runs` / `sync_run_switches` (migration 0002), reconciliation engine with
soft-delete and a mass-removal guard, sync service with advisory-lock serialisation and per-switch
transactions, APScheduler + `nas sync run` CLI, and the VLAN/sync endpoints including
`/vlans/lookup/{vlan_id}`.

### Original plan

## Milestone 2 — VLAN discovery engine (next)

- `drivers/` package: `NetworkDeviceDriver` Protocol, Juniper (PyEZ), and a mock driver
- Tables: `vlans`, `vlan_interfaces`, `sync_runs`, `sync_run_switches`
- Reconciliation: insert new, update changed, mark vanished as `missing` (soft delete)
- **An unreachable switch must never mark its VLANs missing** — records a per-switch failure and a
  `partial` run instead
- Scheduler (APScheduler, configurable interval) + CLI entrypoint, sharing one service layer,
  serialised by a PostgreSQL advisory lock
- Endpoints: `GET /vlans`, `GET /vlans/{id}`, `GET /vlans/lookup/{vlan_id}`, `POST /sync`,
  `GET /sync/status`, `GET /sync/runs`
- Device facts populating `switches.model` / `os_version` / reachability

The mock driver is what makes reconciliation testable in CI, where no switch is reachable.

## Cisco drivers — COMPLETE (2026-08-04)

Inserted ahead of production hardening after the infrastructure team reported the
primary fleet is Cisco (Catalyst 3650, other Catalyst, Nexus 9000) with Juniper
secondary. Hardening a service that could not read the actual estate would have
been the wrong order.

Delivered: `cisco_iosxe` (netmiko SSH + pure CLI parser) and `cisco_nxos` (NX-API
JSON over HTTPS), `DriverOptions`, an optional enable secret, and 85 tests. No
schema migration was required.

**Open questions for the infra team** — none block further work, all improve it:

1. Sample `show vlan brief` / `show vlan | json` from real devices, to replace
   hand-built fixtures with recorded ones.
2. Is `feature nxapi` enabled on the N9Ks? If not, an NX-OS-over-SSH fallback is
   needed.
3. Read-only service accounts, or TACACS with enable? The `enable_password` field
   exists but is untested against a real device.
4. Is VXLAN in use on the Nexus fleet? Determines whether `vn-segment` enrichment
   matters.

## Milestone 3 — Django integration

A new `netops` app in ClientManager. **No models, no migrations** — an HTTP client plus templates, which
is what keeps the two systems genuinely decoupled.

- Typed client: base URL, API key, timeouts, bounded retries, degrading to a visible
  "NAS unreachable" banner rather than a 500
- VLAN search, VLAN detail, switch list, sync status, staff-only "Sync Now"
- Access Center tile under a new "Network" group; mounted at `/network/`
- Settings: `NAS_API_BASE_URL`, `NAS_API_KEY`, `NAS_API_TIMEOUT`, `NAS_API_VERIFY_SSL`

Footprint in the existing codebase: `INSTALLED_APPS`, root `urls.py`, the `access_center` module
list, `requirements.txt`. **No existing app is modified.**

Per the current scope decision, VLAN records will not link to `core.Client` or
`threecx.ThreeCX`. Customer and service attribution shows whatever text the switch reports in the
VLAN description.

## Staging deployment — COMPLETE (2026-08-05)

Deployed to App-Server / DB-Server at Westpoint and verified against a production
WS-C3650-48PD (IOS-XE 16.6.9): 69 VLANs discovered, second run idempotent, availability
lookup answering correctly through the ClientManager UI. See
[deployment.md](deployment.md) and [architecture.md](architecture.md).

Telnet support was scoped and then **dropped** — SSH was enabled on the switch instead,
so the planned `transport` column and telnet driver were never built.

## Milestone 4 — Production hardening

- ~~`audit_log` table and write path~~ — **done (2026-08-06)**. Records `sync.trigger` and
  `auth.denied`, attributed to both the API key and the caller-asserted actor. Readable at
  `GET /api/v1/audit` under a new `audit:read` scope. See [security.md](security.md) for what is
  deliberately *not* recorded and why a failed audit write does not fail the operation.
- Rate limiting
- Integration suite against mock switches
- Staging deployment: Nginx + systemd, runbook
- Full security review of the completed phase
- Refactoring pass informed by what Milestones 2–3 revealed

---

## Phase 2 — VLAN usage mapping and tagging

**Not started, and deliberately not designed in code yet.** Recorded here so the shape is
agreed before anything is built.

### Why it is blocked on hardware, not on us

The one switch NAS currently reads (`switch-01.westpoint`) has 69 VLANs, and **almost none of
them are mapped to anything**. `show vlan brief` reports access ports only, so 68 of the 69 come
back with no member ports — they are trunk-carried, and the trunk config is where the meaning
lives. So "VLAN 1234 is in use" is answerable today; "in use *by what*" is not, because the data
is not on this device in a form we can read.

Gilbert is being given access to further switches that carry full configuration. Until those
arrive there is nothing to build against, and building it against the one switch we have would
produce a feature that looks right in development and reports almost nothing in production.

### Step 1 — Read where a VLAN is actually carried (discovery only)

Extend the drivers to answer "which ports, on which switch, carry this VLAN, and in what mode".

- `show interfaces trunk` on IOS-XE, the NX-OS and Junos equivalents.
- **Expect a one-off churn event.** Interface signatures change once trunk members are
  included, so the next sync after this ships reports *every* Cisco VLAN as `updated` exactly
  once. Not a bug — but it will look alarming in the run history if nobody expects it, and it
  should be called out in the release note.
- Still read-only. This step adds no write path and no new trust.

### Step 2 — Relate VLANs to computes and environments

Once a VLAN's ports are known, the port is the join to what is actually connected — a compute
node, a hypervisor uplink, a customer handoff.

Open questions to settle **before** schema work, because each changes the design materially:

- **Where does the compute/environment inventory live?** If NAS has to hold it, that is a new
  source of truth to keep current, and stale inventory is worse than none. If ClientManager or
  XOA already holds it, NAS should reference rather than copy.
- **What is the join key?** Port + switch is discoverable; MAC or LLDP neighbour is more
  robust but needs more device reads.
- **Is a mapping discovered or asserted?** Discovered mappings can be refreshed and trusted.
  Asserted ones (step 3) are somebody's claim and go stale silently. They must be visually
  distinguishable, or an operator will treat a stale assertion as a fact — the same failure the
  staleness handling on the lookup screen already guards against.

**Constraint that still holds:** no link to `core.Client` or `threecx.ThreeCX` — an explicit
instruction from the start of this work, recorded in [decisions.md](decisions.md).
"Compute/environment" here means infrastructure, not customer records.

### Step 3 — Let support engineers tag a VLAN

Support engineers record which computes or environments a VLAN belongs to, for the cases
discovery cannot see.

This is the first **write** in the whole system, so it needs decisions Phase 1 never had to make:

- **It writes to NAS's database, never to a device.** The read-only switch account stays
  read-only. That boundary is what makes the current design safe to run against production, and
  tagging must not erode it.
- **Every tag needs an author and a timestamp** — the `audit_log` already built covers the
  action; the tag itself needs attribution on the row so the UI can show who claimed what.
- **A new scope** (`vlans:write`) and a genuine authorisation question: support engineers are
  *not* administrators under `_is_admin`, so this is the first netops action that needs a
  privilege between "any logged-in user" and "admin".
- **Conflict handling.** Two engineers tagging the same VLAN, and a tag that contradicts what
  discovery later reports. Discovery should win on facts; the assertion should survive as a
  note rather than being silently overwritten.
- **Editing and removal**, which the audit trail must capture as distinct actions.

### Sequencing

Step 1 is independent and can ship as soon as a fully configured switch is reachable. Step 2 is
blocked on the inventory question, not on code. Step 3 should not start until step 2 settles,
because tagging a thing NAS cannot yet describe would define the data model by accident.

---

## Deliberately out of scope for Phase 1

Not oversights — the brief excludes them, and each would require a change to the trust model or a
new class of risk:

- VLAN mapping, allocation or creation — **now planned as Phase 2 above**, which is where the
  usage mapping and engineer tagging live. Still out of scope for Phase 1.
- Switch or interface configuration
- Any write operation against a network device — note Phase 2 step 3 introduces writes to
  *NAS's own database only*; the read-only device account is not affected
- Linking VLANs to ClientManager clients or 3CX records (deferred by explicit decision, and
  still excluded in Phase 2)
- Vendors other than Juniper (the abstraction is in place; drivers are not)
- Workflow approvals, rollback, multi-site synchronisation

**Nothing in Phase 1 modifies switch configuration.** The switch account NAS uses should be
read-only, which enforces that at the device rather than trusting the code.

## Known follow-ups

| Item | Notes |
|---|---|
| Retire plaintext SSH passwords in `pbx_backups` | [`CXFTPServer.ssh_password`](../../pbx_backups/models.py) is a plaintext column. The `CredentialProvider` pattern is the replacement; worth doing once Milestone 3 proves the approach |
| Full-disk encryption on the NAS host | The credentials file is `0600` but unencrypted at rest |
| Prometheus metrics endpoint | Architecture is compatible; no exporter yet |
| OpenTelemetry tracing | Request ids already provide cross-service correlation |
| Link VLANs to ClientManager records | Deferred from Phase 1 by decision; revisit once discovery is proven |
