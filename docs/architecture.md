# Architecture

## The system in two parts

```
┌───────────────────────────────────┐         ┌──────────────────────────────────────┐
│   Django CRM (anganicrm)          │         │   Network Automation Service (NAS)   │
│                                   │         │                                      │
│   • authentication, 2FA           │  HTTPS  │   • holds switch credentials         │
│   • authorisation, roles          │ ──────► │   • SSH / NETCONF to devices         │
│   • UI, reporting, search         │ X-API-  │   • discovery + reconciliation       │
│   • consumes the NAS API          │  Key    │   • REST API, versioned              │
│                                   │         │                                      │
│   NEVER: SSH, credentials,        │         │   Private network only               │
│          network commands         │         │   IP allowlist + scoped API keys     │
└───────────────────────────────────┘         └──────────────────┬───────────────────┘
                                                                 │ SSH / NETCONF
                                                                 ▼
                                                        ┌────────────────────┐
                                                        │  Juniper switches  │
                                                        │  (read-only acct)  │
                                                        └────────────────────┘
```

The boundary is the point of the design. Everything that can reach a switch lives on one
side of it. The CRM is internet-adjacent and has a large attack surface (sessions, uploads,
email, many user roles); NAS is a small, single-purpose service on a private network with a
narrow API. Putting device credentials only in NAS means a CRM compromise does not become a
network compromise.

## Layers

```
                 ┌─────────────────────────────────────────────┐
   inbound  ──►  │  api/          routers, DTOs, deps          │   HTTP concerns only
                 ├─────────────────────────────────────────────┤
                 │  services/     use-cases, orchestration     │   business rules
                 ├─────────────────────────────────────────────┤
                 │  repositories/ data access behind Protocols  │   persistence
                 │  drivers/      device access (Milestone 2)   │   device I/O
                 ├─────────────────────────────────────────────┤
                 │  domain/       entities, enums, pagination   │   pure, zero imports out
                 └─────────────────────────────────────────────┘
   cross-cutting: core/ (config, logging, errors, security, credentials, middleware)
```

**The dependency rule: imports point inward only.** `domain` imports nothing from `api`,
`services`, `repositories`, `db` or `core`. `services` depend on repository *Protocols*, not
on SQLAlchemy.

This is not decoration. Two concrete payoffs:

1. **The whole API surface is tested without a database.** `tests/api/` overrides the
   repository dependencies with in-memory fakes and exercises real routing, real middleware,
   real auth and real error handlers. 156 tests, no PostgreSQL, ~2 seconds.
2. **Business rules are testable without a switch.** Milestone 2's reconciliation logic
   (created / updated / marked-missing) will be pure functions over domain objects, testable
   deterministically in CI where no device is reachable.

### Where each layer's code lives

| Layer | Module | Responsibility |
|---|---|---|
| API | `api/v1/switches.py` | Route definitions, query validation, scope declaration |
| API | `api/v1/schemas.py` | Response DTOs — the public contract |
| API | `api/deps.py` | The only place outer layers are assembled |
| API | `api/health.py` | Liveness, readiness, health |
| Service | `services/switches.py` | Switch inventory use-cases, credential status derivation |
| Service | `services/auth.py` | Key authentication, scope authorisation |
| Repository | `repositories/protocols.py` | Interfaces + filter/input DTOs |
| Repository | `repositories/switches.py`, `api_keys.py` | SQLAlchemy implementations |
| Domain | `domain/entities.py` | `Switch`, `ApiKey` — immutable, framework-free |
| Domain | `domain/enums.py` | `Vendor`, `CredentialStatus`, `ReachabilityState` |
| Domain | `domain/pagination.py` | `PageRequest`, `Page[T]` |
| Core | `core/config.py` | Settings, boot-time hardening checks |
| Core | `core/credentials.py` | `CredentialProvider` protocol + file implementation |
| Core | `core/security.py` | Key generation, hashing, scopes |
| Core | `core/errors.py` | Error hierarchy + the single response envelope |
| Core | `core/middleware.py` | Request context, IP allowlist, security headers |
| Core | `core/logging.py` | structlog JSON configuration |

## Request flow

A `GET /api/v1/switches` call, in order:

```
1. RequestContextMiddleware   assign/propagate request id, bind log context, start timer
2. IpAllowlistMiddleware      reject if source IP is outside the allowlist       → 403
3. SecurityHeadersMiddleware  (applies headers on the way out)
4. require_scopes dependency  authenticate X-API-Key                             → 401
                              authorise required scopes                          → 403
5. get_session dependency     open one session / one transaction
6. SwitchService              query via repository, derive credential_status
7. SwitchResponse.from_view   map domain → DTO
8. commit, log request_completed with status and duration
```

Middleware order matters and is deliberate. Starlette applies middleware in reverse
registration order, so `RequestContextMiddleware` is registered last to run **first** — which
means an IP rejection is still logged with a correlation id and still returns a request id to
the caller.

Note that the IP check precedes authentication. A caller from a disallowed address gets a
`403` regardless of what key it presents, so it cannot use the API to probe key validity.

## Error handling

One envelope, always:

```json
{"error": {"code": "SWITCH_NOT_FOUND", "message": "...", "request_id": "...", "details": {}}}
```

Four handlers in `core/errors.py` cover every path: `AppError` (deliberate failures),
`RequestValidationError` (422), `StarletteHTTPException` (404 on unknown routes, 405), and a
catch-all `Exception` handler. The catch-all logs a full stack trace but returns a generic
`INTERNAL_ERROR` — implementation details never cross the trust boundary.

Validation errors report the offending field's *location and type* but strip Pydantic's
`input` value, so a malformed secret in a request is never echoed back or logged.

## Configuration

`core/config.py` is a frozen `pydantic-settings` model, `NAS_`-prefixed, parsed once and
cached. It performs **boot-time hardening**: in `staging` or `production`, an empty IP
allowlist raises at startup rather than starting a service that silently exposes the only
component with switch access. Failing loudly at boot beats failing quietly in production.

## Observability

Structured JSON logs via structlog, one object per line. Every record carries `timestamp`,
`level`, `logger`, `service`, and inside a request also `request_id`, `client_ip`, `method`,
`path` and — once authenticated — `api_key_id`. stdlib loggers (uvicorn, SQLAlchemy, Alembic)
are routed through the same formatter so the stream stays uniformly parseable by Loki.

An inbound `X-Request-ID` is honoured and echoed back, so a correlation id set by the CRM
traces a request across both services. Inbound values are length-capped and character-filtered
before use — they land in log records, so they are treated as untrusted input.

Three probes, because they answer different questions: `/live` (process up — restart if
failing), `/ready` (can serve traffic, checks the database — de-pool but do not restart),
`/health` (human/monitoring summary). All three are unauthenticated and exempt from the IP
allowlist so probes need no whitelisting; they expose no switch, customer or credential data.

## Extension points

Adding capability should mean adding a file, not editing the core.

| To add | Do this | Nothing else changes |
|---|---|---|
| A vendor | Implement the driver Protocol, register it (Milestone 2) | Sync engine, API, schema |
| An endpoint | New router module, include it in `api/v1/router.py` | — |
| A secret backend | New `CredentialProvider` implementation + factory line | Drivers, services |
| An API version | New `api/v2/` package under a new prefix | v1 keeps working |
| A persistence change | New repository implementation behind the same Protocol | Services, API |

## What Milestone 2 adds

`vlans`, `vlan_interfaces`, `sync_runs` and `sync_run_switches` tables; a `drivers/` package
with a `NetworkDeviceDriver` Protocol plus Juniper (PyEZ) and mock implementations; a
reconciliation engine with soft-delete; APScheduler plus a CLI entrypoint sharing one service
layer; and the `/vlans`, `/vlans/lookup/{vlan_id}`, `/sync` and `/sync/status` endpoints.

The layering above was chosen so that none of that requires restructuring what exists.

---

## Milestone 2 layers

```
drivers/                     device-facing, vendor-specific
├── base.py                  Protocol + normalised DTOs. Imports no vendor library
├── juniper.py               PyEZ/NETCONF I/O
├── juniper_parser.py        pure XML -> DTO. No PyEZ import, no I/O
├── mock.py                  deterministic in-memory device
└── registry.py              Vendor -> driver. The only file a new vendor touches

sync/reconciler.py           pure diffing. No I/O, no ORM, no driver
services/sync.py             orchestration: lock, per-switch transactions, attribution
services/vlans.py            queries + derived availability verdict
scheduler/runner.py          APScheduler; decides *when*, never *how*
db/locks.py                  advisory lock primitive
```

### Where the safety properties live

The milestone's destructive failure mode is "mark VLANs missing when we simply could not read
the switch". Three separate layers make that hard:

1. **`services/sync.py`** only calls the reconciler *after* `get_vlans()` returns. Any
   `DriverError` jumps straight to recording a failure, so an unreadable switch produces no plan
   at all. This is structural, not a conditional that could be edited away.
2. **`sync/reconciler.py`** refuses to build a plan that would mark *every* active VLAN missing
   because the device reported none — far more likely a silent read failure than a mass deletion.
   Overridable via `NAS_SYNC_ALLOW_EMPTY_DISCOVERY`.
3. **`drivers/base.py`** documents `get_vlans()` as "complete set or raise". A driver returning a
   partial list would look like deletions, so partial returns are a contract violation.

### Why the parser is a separate pure module

`juniper_parser.py` takes XML text and returns DTOs. No PyEZ, no sockets. That makes the riskiest
part of the Juniper integration — reading real device output across Junos versions — testable
against recorded fixtures on a laptop and in CI, with the optional dependency absent.

### Transaction shape

**One transaction per switch, not per run.** A run that fails on switch four keeps the work done
for switches one to three. Combined with `sync_run_switches` rows, a partial run is both durable
and explainable. The advisory lock is held on its own connection for the whole run while each
switch commits independently.

### Why PyEZ is dispatched to a thread

PyEZ is synchronous. Every call goes through `anyio.to_thread.run_sync`; blocking the event loop
would stall every in-flight HTTP request while a switch is polled. Switches are polled
concurrently, bounded by `NAS_SYNC_MAX_CONCURRENCY`.
