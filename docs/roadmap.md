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

A new `netops` app in the CRM. **No models, no migrations** — an HTTP client plus templates, which
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

## Milestone 4 — Production hardening

- `audit_log` table and write path
- Rate limiting
- Integration suite against mock switches
- Staging deployment: Nginx + systemd, runbook
- Full security review of the completed phase
- Refactoring pass informed by what Milestones 2–3 revealed

---

## Deliberately out of scope for Phase 1

Not oversights — the brief excludes them, and each would require a change to the trust model or a
new class of risk:

- VLAN mapping, allocation or creation
- Switch or interface configuration
- Any write operation against a network device
- Linking VLANs to CRM clients or 3CX records (deferred by explicit decision)
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
| Link VLANs to CRM records | Deferred from Phase 1 by decision; revisit once discovery is proven |
