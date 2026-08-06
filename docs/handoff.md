# Handoff — resume here

Last worked: **2026-08-06**. Phase 1 discovery is **live on real hardware**. The
ClientManager rename and docs pass are done, and the first Milestone 4 item — the audit
log — has shipped.

**Next up: rate limiting**, then Nginx. See the Milestone 4 list below.

---

## Current state

| | |
|---|---|
| NAS repo | `nas/` — separate git repo inside the ClientManager working dir, gitignored from it |
| NAS remote | `https://github.com/gilbert-mutai/NAS.git` · `master` / `nas-gilbert` |
| ClientManager | `master` / `gilbert` |
| Quality gate | ruff · `mypy --strict` (63 files) · **588 NAS tests** · **161 ClientManager tests** · no migration drift |
| Staging | live, syncing a production Catalyst 3650 every 15 min |
| ClientManager UI | behind `NETOPS_ENABLED`, **default off** — see below |

### The netops screens are gated

`NETOPS_ENABLED` (ClientManager `.env`) defaults to **False**, so production hides the
feature until it is deliberately switched on. Off means no Access Center tiles *and*
404 on every `/network/` URL — not merely unlinked. Set `NETOPS_ENABLED=True` for local
work and restart `runserver`. Details in `netops/README.md`.

When Milestone 4 finishes and ClientManager's side is deployed, turning this on is the
release switch.

Shipped: Milestones 1–3, the Cisco drivers, the staging deployment, the rename, and the
audit log. See [roadmap.md](roadmap.md).

## The deployment

Diagrammed in [architecture.md](architecture.md). Real addresses:

| Host | Mgmt LAN | Private link |
|---|---|---|
| App-Server | 192.168.95.238 | 10.10.10.238 |
| DB-Server | 192.168.95.241 | 10.10.10.241 |
| switch-01.westpoint | 192.168.95.237 | — |

NAS runs from `/opt/nas/app` as user `nas`, uvicorn on `127.0.0.1:8000` via
`nas.service`. Database `nas` on DB-Server, reachable over the `10.10.10.x` link only.
Switch is a `WS-C3650-48PD`, IOS-XE 16.6.9, **69 VLANs**, read-only account
`nas-readonly` at privilege 1 over SSH.

**venv + systemd, not Docker** — Gilbert's choice, for uniformity with ClientManager. The
Dockerfile stays for CI and parity checks.

### Working on it

```bash
# App-Server. /opt/nas is mode 0700, so run as the nas user and cd *inside* that shell
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas sync run'
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas sync status'
systemctl --no-pager status nas.service
journalctl -u nas.service -f

# From a laptop
ssh -N -L 8126:127.0.0.1:8000 infra-admin@192.168.95.238
# ClientManager .env:  NAS_API_BASE_URL=http://127.0.0.1:8126
```

Two things that will waste your time otherwise:

- **Django does not reload `.env`.** After editing it, kill and restart `runserver`, or
  the UI reports "Network Automation Service is not configured" while the file looks
  correct.
- **Long connection URIs break when pasted** across a terminal line wrap — the newline
  lands inside the password. Build them from a variable:
  `read -rs -p "pw: " PW; psql "postgresql://nas:$PW@10.10.10.241:5432/nas" -c "select 1"`

### Local development

```bash
cd nas
docker compose up -d                      # PostgreSQL on 127.0.0.1:5434
export PATH="$PWD/.venv/bin:$PATH"
export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest                                    # 588
```

Port 5434 is deliberate: 5432 is ClientManager's PostgreSQL, 5433 belongs to an unrelated
container on this machine.

`nas_dev` holds mock switches plus deliberately broken ones — unreachable, missing
credential, legacy `cisco` vendor, Juniper with no PyEZ installed. A local run is
*expected* to be `partial`; that keeps every failure path visible.

```bash
NAS_MOCK_DRIFT=7 nas sync run   # changes mock VLAN sets -> real creates/updates/removals
nas sync run                    # revert -> reactivations + removals
```

Hostname markers inject driver failures: `-unreachable`, `-badauth`, `-garbled`.

---

## Milestone 4 — production hardening

### Done: audit log (2026-08-06)

`audit_log` records `sync.trigger` (success, 409, and unexpected failure) and
`auth.denied`. Attribution is **two columns on purpose** — `api_key_name` is what NAS
authenticated, `actor` is what the caller asserted via `X-Actor` and is not verified.
ClientManager forwards the logged-in user's email on "Sync Now".

Reads are not audited (the access log already has them) and neither are authentication
failures (nothing to attribute, and trivially floodable). Readable at
`GET /api/v1/audit` under a new `audit:read` scope, which is **not** in the read-only
bundle ClientManager holds.

Two behaviours to know before changing it:

- `AuditService` commits on **its own session**, so a rejected sync's entry survives
  the request rollback. Remove that and the 409 stops being recorded.
- A failed audit write **does not fail the request** — it logs at `error` with the
  whole entry inline. By then the switches have been polled, so a 500 would report
  failure for work that succeeded and the retry would poll them again.

**Deployment note:** the migration adds a table; run `nas db upgrade` on App-Server.
Nothing else is needed — the actor header is optional and old callers keep working.

### Still to do

1. **Rate limiting** on `/api/v1`.
2. **Nginx** in front of uvicorn on App-Server, then set
   `NAS_TRUST_PROXY_HEADERS=true` — and not before, or `X-Forwarded-For` becomes
   spoofable past the IP allowlist. Note this also affects `audit_log.source_ip`,
   which uses the same resolution.
3. **Deploy ClientManager's side properly.** It currently reaches NAS through an SSH
   tunnel from a laptop. When ClientManager is deployed, add its host to
   `NAS_ALLOWED_IP_RANGES` as a `/32`.
4. **Revoke the old `crm` API key** in the staging database once nothing uses it:
   `nas apikey list` then `nas apikey revoke crm`. The active key is `clientmanager`.
   Note the audit trail keeps a name snapshot, so revoking does not erase history.
5. **Mint an operator key with `audit:read`** if anyone needs to read the trail
   through the API rather than psql.
6. **Security review** of the whole Phase 1 surface.

## Known gaps, deliberately deferred

- **Trunk membership.** Only VLAN 1 shows ports; the other 68 are trunk-carried, and
  `show vlan brief` reports access ports only. Availability lookup is unaffected and
  accurate — but "where is this VLAN used" is sparse. Fixing needs
  `show interfaces trunk`, and note the cost: interface signatures change, so the next
  sync reports every Cisco VLAN as `updated` exactly once. Not a bug; expect it.
- **Nexus and Juniper drivers have never met real hardware.** Only the Catalyst path has.
  Both are covered by fixtures and `httpx.MockTransport`, which is not the same thing.
- **~96 SSH logins/day** to a production switch from the 15-minute schedule. If infra's
  AAA alerting objects, raise the interval rather than disabling the scheduler.
- **`pbx_backups` plaintext `ssh_password`** in ClientManager. NAS's credential design is
  the replacement pattern, now proven across three platforms.
- **The Django package is still `anganicrm`.** Renaming it touches
  `DJANGO_SETTINGS_MODULE`, systemd units and the deploy pipeline — a separate, riskier
  change, deliberately not bundled with the prose rename.

## Things that have bitten us

Five bugs so far passed both `ruff` and `mypy --strict` and were caught only by running
the code. Patterns worth carrying forward:

- **Assert identity, not counts.** A leaked loop variable made every reconciliation plan
  entry reference the wrong record; every count was correct.
- **Use recorded output, not hand-written fixtures.** `parse_version` worked on a
  single-line banner and failed on the wrapped output real devices emit. The real 3650
  output now lives in `tests/fixtures/`.
- **Never annotate a FastAPI parameter with a closure variable.** Every module here uses
  `from __future__ import annotations`, so annotations are strings that FastAPI resolves
  against the *module* namespace. `Depends(some_local)` inside a dependency factory
  raises an unresolved ForwardRef during schema generation, and FastAPI degrades the
  parameter into a request body — a 422 on every call to the route. `audited_context`
  calls `require_scopes` directly instead, and says so in a comment. Annotate only
  module-level names.

Also: **a local test run is not automatically equivalent to CI.** A local `.env` with
`DEBUG=True` masked a staticfiles-manifest failure that turned CI red. `settings_ci` now
pins `DEBUG=False` so the two match.

And: **editing with `sed` after `ruff format` has run** is how two of those bugs were
introduced — a pattern that no longer matches fails silently and leaves half a rename
behind. Prefer targeted edits against current file contents. When scripting an edit,
assert the replacement actually applied — a mutation test that silently no-ops looks
exactly like a passing test.

Finally: **mutation-test a test before trusting it.** The audit suite's
"identical timestamps paginate correctly" test passes with or without the `id DESC`
tiebreak it was written to protect — PostgreSQL happens to return equal keys in reverse
insertion order at this table size. The tiebreak stays because SQL does not promise
that, but the test's docstring now says plainly that it would not catch the removal.
An untested guard that reads like a tested one is worse than no test.

## Reading order for a cold start

1. This file
2. [architecture.md](architecture.md) — topology diagram, layering, where safety lives
3. [decisions.md](decisions.md) — why things are as they are, including the Cisco pivot
4. [deployment.md](deployment.md) — matches what is actually running
5. [testing.md](testing.md) — what "done" means
