# Handoff — resume here

Last worked: **2026-08-04**. Milestones 1 and 2 complete.

---

## Where things stand

| | |
|---|---|
| Repo | `nas/` — separate git repo inside the CRM working directory, gitignored from it |
| Remote | `https://github.com/gilbert-mutai/NAS.git` |
| `master` | `1ddceb4` — Milestone 1. Pushed. |
| `nas-gilbert` | Current branch. Milestone 2 work. |
| Quality gate | ruff clean · `mypy --strict` clean (54 files) · **417 tests** · no migration drift · `pip-audit` clean |
| Schema | `0002_vlan_discovery` applied |

Milestone 2 delivered: driver abstraction (Juniper PyEZ + mock + registry), a pure XML parser,
VLAN and sync-run tables, the reconciliation engine, the sync service, APScheduler + CLI, and the
VLAN/sync endpoints including `/vlans/lookup/{vlan_id}`.

### Uncommitted

Everything from Milestone 2. Check `git status` — none of it is committed yet.

---

## Restarting the environment

```bash
cd ~/Documents/Personal/Projects/ClientManager/nas
docker compose up -d                       # PostgreSQL on 127.0.0.1:5434
export PATH="$PWD/.venv/bin:$PATH"

nas db current                             # expect 0002_vlan_discovery
nas switch list
nas sync run                               # exits 1 on a partial run, by design
nas sync status

export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest                                     # 417
```

**Port 5434 is deliberate.** 5432 is the CRM's PostgreSQL; 5433 is taken by an unrelated
`isp_postgres` container on this machine.

`nas_dev` holds mock switches (`mock-adc-core`, `mock-icolo-core`, `mock-mba-edge`), one
deliberately unreachable (`mock-dead-sw`), one unsupported vendor (`mba-edge-sw1`, cisco) and one
Juniper switch with no reachable device (`adc-core-sw1`). That mix keeps success, failure and skip
paths exercised — a run is *expected* to be `partial` locally.

### Local experiments worth knowing

```bash
NAS_MOCK_DRIFT=7 nas sync run   # changes the mock VLAN sets -> real creates/updates/removals
nas sync run                    # revert the drift -> reactivations + removals
```

Hostname markers inject driver failures: `-unreachable`, `-badauth`, `-garbled`.

---

## Milestone 3 — Django `netops` app

The next milestone, and the first one users will see.

### Hard constraints

- **No models, no migrations.** The app is an HTTP client plus templates. That is what keeps the
  two systems decoupled.
- **No foreign keys to `core.Client` or `threecx.ThreeCX`.** Gilbert deferred this explicitly to
  avoid coupling a new service to the CRM's most load-bearing model. It is a decision, not an
  oversight. Customer attribution shows whatever text the switch reports.
- Total permitted CRM footprint: `INSTALLED_APPS`, root `urls.py`, the `access_center` module list
  in `core/views.py`, `requirements.txt`.

### Suggested order

1. **`netops/client.py`** — a typed `requests.Session` wrapper. Base URL and API key from settings,
   a timeout on every call, bounded retries, and a `NASUnavailable` exception. The CRM must stay
   usable when NAS is down: degrade to a visible banner, never a 500.
2. **Settings** — `NAS_API_BASE_URL`, `NAS_API_KEY`, `NAS_API_TIMEOUT`, `NAS_API_VERIFY_SSL`.
   Mint the key with
   `nas apikey create --name crm --scopes vlans:read,switches:read,sync:read,sync:write`.
3. **VLAN search page** — the primary screen. Wraps `GET /api/v1/vlans`.
4. **VLAN lookup** — wraps `/vlans/lookup/{vlan_id}`. This is the screen that replaces SSH.
   **Surface `is_stale` prominently**: an `available` verdict on stale data may be wrong, and an
   engineer acting on it would double-assign a VLAN. Show `data_as_of` alongside.
5. **Switch list** + **sync status**, with a staff-only "Sync Now" posting to `/api/v1/sync`.
   Handle `409` as an informational message, not an error.
6. **Access Center tile** under a new "Network" group, mounted at `/network/`.
7. Forward the Django request id as `X-Request-ID` so a user-reported problem traces across both
   services.

### Milestone 4

Audit log, rate limiting, mock-switch integration suite in CI, Nginx + systemd staging deploy,
security review. See [roadmap.md](roadmap.md).

---

## Things to be careful about

**Two bugs in Milestone 2 passed both `ruff` and `mypy --strict`** and were only caught by running
the service. Both now have regression tests, but the pattern is worth remembering:

1. A leaked loop variable made every reconciliation plan entry point at the wrong record. Static
   checks passed because the leaked name was a valid `Vlan`. Assert *identity*, not just counts.
2. `mark_used` held an API-key row lock for the whole request, serialising every request sharing a
   key behind the slowest one. Only visible under genuine concurrency with timing.

**Editing files with `sed`/string replacement after `ruff format` has run is how both were
introduced.** A pattern that no longer matches fails silently and leaves half a rename in place.
Prefer targeted edits against the current file contents, and re-read before patching.

## Reading order for a cold start

1. This file
2. [architecture.md](architecture.md) — layering, and where the safety properties live
3. [decisions.md](decisions.md) — why things are as they are, before changing any of them
4. [testing.md](testing.md) — what "done" means, and the isolation gotcha
5. [api.md](api.md) — the contract Milestone 3 consumes
