"""Operator CLI.

Administrative actions deliberately live here rather than behind HTTP endpoints.
Issuing an API key or registering a switch requires shell access to the host, so
a compromised API key cannot escalate into minting more keys or redirecting NAS
at an attacker-controlled device.

    nas apikey create --name clientmanager --scopes switches:read,vlans:read,sync:read
    nas switch add --name adc-core-sw1 --hostname 10.20.0.11 --vendor juniper \
                   --credential-ref juniper-core
    nas credentials check
    nas db upgrade
    nas serve --reload
"""

from __future__ import annotations

import asyncio
import getpass
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from nas.core.config import Settings, get_settings
from nas.core.credentials import CredentialError, build_credential_provider
from nas.core.logging import configure_logging
from nas.core.security import Scope, generate_api_key
from nas.db.session import Database
from nas.domain.entities import SyncRun
from nas.domain.enums import SyncTrigger, Vendor
from nas.domain.pagination import PageRequest
from nas.repositories.api_keys import SqlAlchemyApiKeyRepository
from nas.repositories.protocols import NewApiKey, NewSwitch, SwitchFilters
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.services.audit import AuditContext

app = typer.Typer(
    name="nas",
    help="Network Automation Service operator CLI.",
    no_args_is_help=True,
    add_completion=False,
)
apikey_app = typer.Typer(name="apikey", help="Manage API keys.", no_args_is_help=True)
switch_app = typer.Typer(name="switch", help="Manage switch inventory.", no_args_is_help=True)
db_app = typer.Typer(name="db", help="Database migrations.", no_args_is_help=True)
credentials_app = typer.Typer(
    name="credentials", help="Inspect the credential store.", no_args_is_help=True
)
app.add_typer(apikey_app)
app.add_typer(switch_app)
app.add_typer(db_app)
app.add_typer(credentials_app)


def _load_settings() -> Settings:
    try:
        settings = get_settings()
    except Exception as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    # Human-readable logs for interactive use; JSON stays the default for the service.
    configure_logging(settings.model_copy(update={"log_format": "console"}))
    return settings


def _cli_audit_context() -> AuditContext:
    """Attribute a CLI action to the human who invoked it.

    This matters more than the API path. A `nas sync run` reaches a production switch
    from a shell, and the process runs as the service account — so without this the
    audit trail would record the run with no actor at all, and the least supervised
    route to a device would be the only unattributed one.

    ``SUDO_USER`` is preferred because the command is documented as
    `sudo -u nas ...`: the process user is `nas`, and the interesting identity is
    whoever escalated. Falls back to the login name, then to None rather than a
    guess — an unattributed entry is honest, a wrong one is not.

    Advisory like every actor: an operator can set `SUDO_USER` to anything. What NAS
    can state as fact is that the action came from the host, which `source_ip` records.
    """
    actor = os.environ.get("SUDO_USER") or ""
    if not actor:
        try:
            actor = getpass.getuser()
        except Exception:  # pragma: no cover - no passwd entry and no env vars
            actor = ""
    return AuditContext(actor=actor or None, source_ip="cli")


def _run[T](coro_factory: Callable[[Database], Awaitable[T]]) -> T:
    """Run an async command against a short-lived engine."""
    settings = _load_settings()

    async def runner() -> T:
        database = Database(settings)
        try:
            return await coro_factory(database)
        finally:
            await database.dispose()

    return asyncio.run(runner())


def _parse_scopes(raw: str) -> frozenset[str]:
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    if not requested:
        raise typer.BadParameter("At least one scope is required.")
    unknown = sorted(requested - Scope.values())
    if unknown:
        allowed = ", ".join(sorted(Scope.values()))
        raise typer.BadParameter(f"Unknown scope(s): {', '.join(unknown)}. Allowed: {allowed}")
    return frozenset(requested)


# ── API keys ──────────────────────────────────────────────────────────────────
@apikey_app.command("create")
def apikey_create(
    name: Annotated[str, typer.Option(help="Unique name for this key, e.g. 'clientmanager'.")],
    scopes: Annotated[str, typer.Option(help="Comma-separated scopes. See 'nas apikey scopes'.")],
    description: Annotated[str | None, typer.Option(help="Free-text note.")] = None,
    expires_days: Annotated[
        int | None, typer.Option(help="Expire the key after N days. Omit for no expiry.")
    ] = None,
) -> None:
    """Create an API key and print it once."""
    parsed_scopes = _parse_scopes(scopes)
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_days) if expires_days is not None else None
    )
    generated = generate_api_key()

    async def action(database: Database) -> None:
        async with database.session() as session:
            await SqlAlchemyApiKeyRepository(session).create(
                NewApiKey(
                    name=name,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    scopes=parsed_scopes,
                    description=description,
                    expires_at=expires_at,
                )
            )

    _run(action)

    typer.secho(f"\nAPI key '{name}' created.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  scopes  : {', '.join(sorted(parsed_scopes))}")
    typer.echo(f"  expires : {expires_at.isoformat() if expires_at else 'never'}")
    typer.echo("\n  Key (shown once — store it now, it cannot be recovered):\n")
    typer.secho(f"    {generated.plaintext}\n", fg=typer.colors.YELLOW, bold=True)
    typer.echo("  Set it on the Django side as NAS_API_KEY.\n")


@apikey_app.command("list")
def apikey_list() -> None:
    """List API keys. Never prints key material."""

    async def action(database: Database) -> None:
        async with database.session() as session:
            keys = await SqlAlchemyApiKeyRepository(session).list_all()

        if not keys:
            typer.echo("No API keys. Create one with: nas apikey create --name ... --scopes ...")
            return

        typer.echo(f"{'NAME':<20} {'PREFIX':<10} {'ACTIVE':<7} {'EXPIRES':<22} SCOPES")
        for key in keys:
            expires = key.expires_at.isoformat() if key.expires_at else "never"
            active = "yes" if key.is_usable() else "no"
            typer.echo(
                f"{key.name:<20} {key.prefix:<10} {active:<7} {expires:<22} "
                f"{', '.join(sorted(key.scopes))}"
            )

    _run(action)


@apikey_app.command("revoke")
def apikey_revoke(name: Annotated[str, typer.Argument(help="Name of the key to revoke.")]) -> None:
    """Revoke an API key. Takes effect on the next request."""

    async def action(database: Database) -> bool:
        async with database.session() as session:
            return await SqlAlchemyApiKeyRepository(session).revoke(name)

    if _run(action):
        typer.secho(f"API key '{name}' revoked.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No active API key named '{name}'.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)


@apikey_app.command("scopes")
def apikey_scopes() -> None:
    """List available scopes."""
    for scope in Scope:
        typer.echo(f"  {scope.value}")


# ── Switches ──────────────────────────────────────────────────────────────────
@switch_app.command("add")
def switch_add(
    name: Annotated[str, typer.Option(help="Unique switch name.")],
    hostname: Annotated[str, typer.Option(help="IP address or DNS name.")],
    vendor: Annotated[Vendor, typer.Option(help="Device vendor.")],
    credential_ref: Annotated[
        str, typer.Option(help="Credential NAME from the credentials file — not a password.")
    ],
    port: Annotated[int, typer.Option(help="SSH/NETCONF port.")] = 22,
    site: Annotated[str | None, typer.Option(help="Site or POP, e.g. 'ADC NBO'.")] = None,
    environment: Annotated[str | None, typer.Option(help="Environment label.")] = None,
    description: Annotated[str | None, typer.Option(help="Free-text note.")] = None,
) -> None:
    """Register a switch in the inventory."""
    if not vendor.is_implemented:
        typer.secho(
            f"Note: no driver is implemented for {vendor.label} yet. The switch will be "
            "registered but skipped during sync.",
            fg=typer.colors.YELLOW,
        )

    async def action(database: Database) -> int:
        async with database.session() as session:
            created = await SqlAlchemySwitchRepository(session).create(
                NewSwitch(
                    name=name,
                    hostname=hostname,
                    vendor=vendor,
                    credential_ref=credential_ref,
                    port=port,
                    site=site,
                    environment=environment,
                    description=description,
                )
            )
            return created.id

    switch_id = _run(action)
    typer.secho(f"Switch '{name}' registered with id {switch_id}.", fg=typer.colors.GREEN)

    settings = _load_settings()
    try:
        provider = build_credential_provider(settings)
    except CredentialError as exc:
        typer.secho(f"Warning: credential store unusable — {exc}", fg=typer.colors.YELLOW)
        return
    if not provider.has(credential_ref):
        typer.secho(
            f"Warning: credential '{credential_ref}' does not resolve. Add it to the "
            "credentials file before running a sync.",
            fg=typer.colors.YELLOW,
        )


@switch_app.command("list")
def switch_list() -> None:
    """List registered switches."""

    async def action(database: Database) -> None:
        async with database.session() as session:
            page = await SqlAlchemySwitchRepository(session).list(
                filters=SwitchFilters(), page_request=PageRequest(page=1, page_size=200)
            )

        if not page.items:
            typer.echo("No switches. Add one with: nas switch add --name ... --hostname ...")
            return

        typer.echo(
            f"{'ID':<5} {'NAME':<24} {'HOSTNAME':<20} {'VENDOR':<10} "
            f"{'SITE':<14} {'ACTIVE':<7} CREDENTIAL REF"
        )
        for switch in page.items:
            typer.echo(
                f"{switch.id:<5} {switch.name:<24} {switch.hostname:<20} "
                f"{switch.vendor.value:<10} {(switch.site or '-'):<14} "
                f"{('yes' if switch.is_active else 'no'):<7} {switch.credential_ref}"
            )

    _run(action)


# ── Credentials ───────────────────────────────────────────────────────────────
@credentials_app.command("check")
def credentials_check() -> None:
    """Verify the credential store loads and that every switch reference resolves.

    Prints credential names only. No secret is ever displayed.
    """
    settings = _load_settings()
    try:
        provider = build_credential_provider(settings)
    except CredentialError as exc:
        typer.secho(f"Credential store unusable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    refs = provider.refs()
    typer.echo(f"Credential store: {settings.credentials_file or '(not configured)'}")
    typer.echo(f"Credentials loaded: {len(refs)}")
    for ref in sorted(refs):
        typer.echo(f"  - {ref}")

    async def action(database: Database) -> list[tuple[str, str]]:
        async with database.session() as session:
            page = await SqlAlchemySwitchRepository(session).list(
                filters=SwitchFilters(), page_request=PageRequest(page=1, page_size=500)
            )
        return [(s.name, s.credential_ref) for s in page.items]

    unresolved = [(switch_name, ref) for switch_name, ref in _run(action) if not provider.has(ref)]
    if unresolved:
        typer.secho("\nSwitches with unresolved credentials:", fg=typer.colors.RED)
        for switch_name, ref in unresolved:
            typer.echo(f"  - {switch_name} -> '{ref}' not found")
        raise typer.Exit(code=1)

    typer.secho("\nAll switch credential references resolve.", fg=typer.colors.GREEN)


# ── Migrations ────────────────────────────────────────────────────────────────
def _alembic_config() -> object:
    from alembic.config import Config

    # Ensures migrations use NAS_DATABASE_URL via env.py, not a URL in a file.
    _load_settings()
    return Config("alembic.ini")


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply migrations."""
    from alembic import command

    command.upgrade(_alembic_config(), revision)  # type: ignore[arg-type]
    typer.secho(f"Database upgraded to {revision}.", fg=typer.colors.GREEN)


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision, e.g. -1.")],
) -> None:
    """Revert migrations."""
    from alembic import command

    command.downgrade(_alembic_config(), revision)  # type: ignore[arg-type]
    typer.secho(f"Database downgraded to {revision}.", fg=typer.colors.GREEN)


@db_app.command("current")
def db_current() -> None:
    """Show the applied revision."""
    from alembic import command

    command.current(_alembic_config(), verbose=True)  # type: ignore[arg-type]


# ── Serve ─────────────────────────────────────────────────────────────────────
@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code change (development).")] = False,
) -> None:
    """Run the API server.

    Defaults to binding loopback only. Exposing the service is a deliberate act
    (``--host 0.0.0.0``), and in staging/production it belongs behind Nginx.
    """
    import uvicorn

    uvicorn.run(
        "nas.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_config=None,  # structlog owns logging configuration
    )


if __name__ == "__main__":
    app()


# ── Sync ──────────────────────────────────────────────────────────────────────
sync_app = typer.Typer(name="sync", help="Run and inspect synchronisation.", no_args_is_help=True)
app.add_typer(sync_app)


@sync_app.command("run")
def sync_run(
    switch: Annotated[
        list[int] | None,
        typer.Option(help="Restrict to these switch ids. Repeatable. Omit for all."),
    ] = None,
) -> None:
    """Run a synchronisation now.

    Shares the SyncService and the advisory lock with the API, so this is safe to
    drive from a systemd timer alongside a running service — set
    NAS_SYNC_ENABLED=false to use timers instead of the embedded scheduler.
    """
    settings = _load_settings()

    async def action(database: Database) -> SyncRun:
        from nas.core.credentials import build_credential_provider
        from nas.services.audit import AuditService
        from nas.services.sync import SyncOptions, SyncService

        service = SyncService(
            session_factory=database.session_factory,
            credential_provider=build_credential_provider(settings),
            audit=AuditService(database.session_factory),
            options=SyncOptions(
                max_concurrency=settings.sync_max_concurrency,
                allow_empty_discovery=settings.sync_allow_empty_discovery,
                connect_timeout=settings.driver_connect_timeout,
                command_timeout=settings.driver_command_timeout,
                stale_run_minutes=settings.sync_stale_run_minutes,
                verify_device_tls=settings.driver_verify_tls,
            ),
        )
        return await service.run(
            trigger=SyncTrigger.CLI,
            audit_context=_cli_audit_context(),
        )

    from nas.services.sync import SyncAlreadyRunningError

    try:
        run = _run(action)
    except SyncAlreadyRunningError:
        typer.secho(
            "A synchronisation is already in progress. Nothing started.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1) from None

    colour = {
        "success": typer.colors.GREEN,
        "partial": typer.colors.YELLOW,
        "failed": typer.colors.RED,
    }.get(run.status.value, typer.colors.WHITE)

    typer.secho(f"\nRun #{run.id} — {run.status.value.upper()}", fg=colour, bold=True)
    typer.echo(f"  duration : {run.duration_ms} ms")
    typer.echo(
        f"  switches : {run.switches_succeeded} ok, {run.switches_failed} failed, "
        f"{run.switches_skipped} skipped"
    )
    typer.echo(
        f"  vlans    : {run.vlans_created} created, {run.vlans_updated} updated, "
        f"{run.vlans_unchanged} unchanged, {run.vlans_marked_missing} marked missing"
    )

    if run.switch_results:
        header = f"  {'SWITCH':<24} {'OUTCOME':<9} {'DISC':>5} {'NEW':>4} {'UPD':>4} {'MISS':>5}"
        typer.echo(f"\n{header}")
        for item in run.switch_results:
            typer.echo(
                f"  {item.switch_name:<24} {item.outcome.value:<9} "
                f"{item.vlans_discovered:>5} {item.vlans_created:>4} "
                f"{item.vlans_updated:>4} {item.vlans_marked_missing:>5}"
            )
            if item.error_message:
                typer.secho(f"      -> {item.error_message}", fg=typer.colors.RED)

    typer.echo()
    # Non-zero exit on anything other than a clean run, so a systemd timer or CI
    # step surfaces the problem instead of silently succeeding.
    if run.status.value != "success":
        raise typer.Exit(code=1)


@sync_app.command("status")
def sync_status() -> None:
    """Show the most recent synchronisation run."""

    async def action(database: Database) -> SyncRun | None:
        from nas.repositories.sync_runs import SqlAlchemySyncRunRepository

        async with database.session() as session:
            return await SqlAlchemySyncRunRepository(session).latest()

    run = _run(action)
    if run is None:
        typer.echo("No synchronisation has run yet. Start one with: nas sync run")
        return

    typer.echo(f"Run #{run.id}  status={run.status.value}  trigger={run.trigger.value}")
    typer.echo(f"  started  : {run.started_at.isoformat()}")
    typer.echo(f"  finished : {run.finished_at.isoformat() if run.finished_at else '(running)'}")
    typer.echo(
        f"  switches : {run.switches_succeeded} ok, {run.switches_failed} failed, "
        f"{run.switches_skipped} skipped"
    )
    typer.echo(
        f"  vlans    : {run.vlans_created} created, {run.vlans_updated} updated, "
        f"{run.vlans_marked_missing} marked missing"
    )
    if run.error_message:
        typer.secho(f"  error    : {run.error_message}", fg=typer.colors.RED)
