# Network Automation Service (NAS)

Backend service that owns all programmatic access to network devices. It authenticates to
switches, discovers their state, stores it in PostgreSQL, and serves it over a versioned
REST API.

Its purpose is to remove the need for engineers to SSH into production switches for routine
provisioning. Consumers — currently Angani ClientManager — read from this API and never touch a
switch directly, never hold switch credentials, and never execute network commands.

**Status: Phase 1 discovery is live on real hardware.** Deployed at Westpoint and synchronising a
production Catalyst 3650 (69 VLANs) every 15 minutes. ClientManager consumes it through its
`netops` app. Remaining: Milestone 4 hardening — audit log, rate limiting, Nginx, security review.

See [the architecture diagram](docs/architecture.md) for the deployed topology.

---

## Contents

| Document | Covers |
|---|---|
| **[docs/handoff.md](docs/handoff.md)** | **Start here when resuming** — current state, how to restart the environment, what is next |
| [docs/architecture.md](docs/architecture.md) | **Topology and sequence diagrams**, layering, dependency rules, the driver pattern |
| [docs/schema.md](docs/schema.md) | Tables, columns, constraints, migration policy |
| [docs/api.md](docs/api.md) | Endpoints, auth, scopes, response and error contracts |
| [docs/security.md](docs/security.md) | Trust boundary, credential handling, threat notes, review |
| [docs/deployment.md](docs/deployment.md) | Local, Docker, and staging deployment (Nginx + systemd) |
| [docs/testing.md](docs/testing.md) | Test strategy, layers, how to run each |
| [docs/decisions.md](docs/decisions.md) | Design decisions and the reasoning behind them |
| [docs/roadmap.md](docs/roadmap.md) | Milestones, current scope boundary, what is deliberately absent |

---

## Why this is a separate service

ClientManager handles authentication, authorisation, UI and business workflow. NAS handles
device access. The split exists so that **the credentials that can reach production switches
live in exactly one process, on one host, inside the private network** — not in the
web-facing application. A compromise of ClientManager does not yield switch access.

The two communicate only over HTTP, with a scoped API key and an IP allowlist.

---

## Quick start (local)

Requires Python 3.12+ and Docker (for PostgreSQL).

```bash
# 1. PostgreSQL on port 5434 (5432 is ClientManager's, 5433 may be in use)
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

# 6. An API key for ClientManager. Printed once — copy it now.
nas apikey create --name clientmanager --scopes switches:read,vlans:read,sync:read,sync:write

# 7. Register switches. --credential-ref is a NAME from credentials.yaml, never a
#    password. Note the platform values and the Nexus port.
nas switch add --name adc-cat-sw1 --hostname 10.20.0.11 \
               --vendor cisco_iosxe --credential-ref cisco-catalyst \
               --site "ADC NBO" --environment production

nas switch add --name adc-n9k-1 --hostname 10.20.0.21 --port 443 \
               --vendor cisco_nxos --credential-ref cisco-nexus \
               --site "ADC NBO" --environment production

nas switch add --name adc-jun-sw1 --hostname 10.20.0.31 \
               --vendor juniper --credential-ref juniper-core --site "ADC NBO"

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
nas apikey create --name clientmanager --scopes switches:read,vlans:read   # prints the key once
nas apikey list                                                  # never prints key material
nas apikey revoke clientmanager                                            # effective on next request
nas apikey scopes

nas switch add --name sw1 --hostname 10.0.0.1 --vendor cisco_iosxe --credential-ref cisco-catalyst
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
embedded scheduler and use timers instead — matching how ClientManager schedules its own jobs.

---

## Supported platforms

| `--vendor` | Devices | Transport | Install |
|---|---|---|---|
| `cisco_iosxe` | Catalyst 3650, 9300, 2960, … | SSH CLI, port 22 | `pip install '.[cisco]'` |
| `cisco_nxos` | Nexus 9000 | **NX-API, JSON over HTTPS, port 443** | core only |
| `juniper` | EX, QFX | NETCONF, port 22 | `pip install '.[juniper]'` |
| `mock` | none — local development and CI | none | core only |

Install both device libraries at once with `pip install '.[devices]'`.

Three things that catch people out:

- **Nexus needs `--port 443`**, not 22. Registering one on 22 fails with the exact
  re-registration command rather than silently connecting elsewhere.
- **`--vendor cisco` is not a thing.** Catalyst and Nexus need different drivers, so
  the value must be `cisco_iosxe` or `cisco_nxos`. The legacy `cisco` value still
  loads but is skipped during sync with a message saying which to use.
- **Nexus requires `feature nxapi`** on the device, and an account with at least the
  `network-operator` role.

Interface mode is reported as `unknown` on Cisco: neither `show vlan brief` nor
`show vlan | json` reliably distinguishes access from trunk, and a consistent
`unknown` is better than a guess. See [docs/decisions.md](docs/decisions.md).

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

This is a **separate git repository** that lives inside ClientManager working directory for
development convenience. `/nas/` is in ClientManager's `.gitignore`, so it can never be committed
into ClientManager's history. The two projects share no code — only an HTTP contract.
