# Network Automation Service (NAS)

Backend service that owns all programmatic access to network devices. It authenticates to
switches, discovers their state, stores it in PostgreSQL, and serves it over a versioned
REST API.

Its purpose is to remove the need for engineers to SSH into production switches for routine
provisioning. Consumers — currently the Angani CRM — read from this API and never touch a
switch directly, never hold switch credentials, and never execute network commands.

**Status: Milestone 1 complete.** Device inventory, API authentication and the operational
spine (config, logging, error handling, health probes, migrations) are done and verified.
VLAN discovery is Milestone 2.

---

## Contents

| Document | Covers |
|---|---|
| **[docs/handoff.md](docs/handoff.md)** | **Start here when resuming** — current state, how to restart the environment, the Milestone 2 plan |
| [docs/architecture.md](docs/architecture.md) | Layering, dependency rules, request flow, extension points |
| [docs/schema.md](docs/schema.md) | Tables, columns, constraints, migration policy |
| [docs/api.md](docs/api.md) | Endpoints, auth, scopes, response and error contracts |
| [docs/security.md](docs/security.md) | Trust boundary, credential handling, threat notes, review |
| [docs/deployment.md](docs/deployment.md) | Local, Docker, and staging deployment (Nginx + systemd) |
| [docs/testing.md](docs/testing.md) | Test strategy, layers, how to run each |
| [docs/decisions.md](docs/decisions.md) | Design decisions and the reasoning behind them |
| [docs/roadmap.md](docs/roadmap.md) | Milestones, current scope boundary, what is deliberately absent |

---

## Why this is a separate service

The Django CRM handles authentication, authorisation, UI and business workflow. NAS handles
device access. The split exists so that **the credentials that can reach production switches
live in exactly one process, on one host, inside the private network** — not in the
web-facing application. A compromise of the CRM does not yield switch access.

The two communicate only over HTTP, with a scoped API key and an IP allowlist.

---

## Quick start (local)

Requires Python 3.12+ and Docker (for PostgreSQL).

```bash
# 1. PostgreSQL on port 5434 (5432 is the CRM's, 5433 may be in use)
docker compose up -d

# 2. Virtualenv and dependencies
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PATH="$PWD/.venv/bin:$PATH"

# 3. Configuration
cp .env.example .env

# 4. Device credentials — kept out of the database entirely
cp credentials.example.yaml credentials.yaml
chmod 600 credentials.yaml

# 5. Schema
nas db upgrade

# 6. An API key for the CRM. Printed once — copy it now.
nas apikey create --name crm --scopes switches:read,vlans:read,sync:read,sync:write

# 7. Register a switch. --credential-ref is a NAME from credentials.yaml, not a password.
nas switch add --name adc-core-sw1 --hostname 10.20.0.11 \
               --vendor juniper --credential-ref juniper-core \
               --site "ADC NBO" --environment production

# 7b. No switch to hand? Register a mock one — it opens no socket and returns
#     deterministic VLANs, so the whole pipeline works on a laptop.
nas switch add --name mock-core --hostname 10.90.0.11 \
               --vendor mock --credential-ref mock-local --site "ADC NBO"

# 8. Discover VLANs
nas sync run          # exit code 1 if any switch failed, for systemd timers and CI
nas sync status

# 9. Run it
nas serve --reload
```

Then:

```bash
curl localhost:8000/health
KEY="nas_..."
curl -H "X-API-Key: $KEY" localhost:8000/api/v1/switches
curl -H "X-API-Key: $KEY" "localhost:8000/api/v1/vlans?q=sip"

# The call that replaces an SSH session: is this VLAN free, and if not, who has it?
curl -H "X-API-Key: $KEY" localhost:8000/api/v1/vlans/lookup/1234

open http://localhost:8000/docs      # Swagger UI
```

**On that last call:** `availability` is derived from current records, never stored. Check
`is_stale` before trusting an `available` verdict — the switches remain authoritative and NAS
holds a synchronised cache.

---

## Operator CLI

Administrative actions are CLI-only, never HTTP endpoints. Minting a key or registering a
switch requires shell access to the host, so a leaked API key cannot be used to mint more
keys or to point NAS at an attacker-controlled device.

```bash
nas apikey create --name crm --scopes switches:read,vlans:read   # prints the key once
nas apikey list                                                  # never prints key material
nas apikey revoke crm                                            # effective on next request
nas apikey scopes

nas switch add --name sw1 --hostname 10.0.0.1 --vendor juniper --credential-ref juniper-core
nas switch list

nas credentials check     # verifies every switch's credential ref resolves; prints names only

nas sync run                          # sync now. Exit code 1 if any switch failed
nas sync run --switch 3 --switch 4    # restrict to specific switches
nas sync status                       # most recent run

nas db upgrade
nas db downgrade -1
nas db current

nas serve --host 127.0.0.1 --port 8000 --reload
```

`nas sync run` shares the SyncService and the advisory lock with the running API, so it is safe to
drive from a systemd timer alongside the service. Set `NAS_SYNC_ENABLED=false` to disable the
embedded scheduler and use timers instead — matching how the CRM schedules its own jobs.

---

## Development

```bash
ruff check . && ruff format .    # lint + format
mypy                            # strict type checking
pytest                          # unit + API tests, no database needed

# Integration tests against real PostgreSQL
docker exec nas-postgres psql -U nas -d nas_dev -c "CREATE DATABASE nas_test OWNER nas;"
export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest -m integration
```

See [docs/testing.md](docs/testing.md) for the full strategy.

---

## Repository layout

```
src/nas/
├── api/           HTTP layer — routers, DTOs, dependency wiring
├── core/          config, logging, errors, security, credentials, middleware
├── domain/        entities and enums. Pure: no I/O, no framework imports
├── services/      use-cases (SwitchService, AuthenticationService)
├── repositories/  data access behind Protocols
├── db/            SQLAlchemy models, session, Alembic migrations
├── cli.py         operator CLI
└── main.py        application factory
tests/
├── unit/          pure logic, no I/O
├── api/           full app via in-memory repositories, no database
└── integration/   real PostgreSQL (opt-in)
```

Dependencies point inward only: `api → services → repositories → domain`. Nothing in
`domain` imports from an outer layer, which is what makes the business rules testable
without a database or a switch.

---

## A note on this repository's location

This is a **separate git repository** that lives inside the CRM working directory for
development convenience. `/nas/` is in the CRM's `.gitignore`, so it can never be committed
into the CRM's history. The two projects share no code — only an HTTP contract.
