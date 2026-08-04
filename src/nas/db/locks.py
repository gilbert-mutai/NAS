"""PostgreSQL advisory locks, used to serialise synchronisation runs.

Two concurrent syncs over the same switches would race on the same VLAN rows and
could interleave into a nonsensical result — one run marking a VLAN missing while
another is re-creating it. A session-scoped advisory lock is the right tool: it
costs no table, needs no cleanup row, and PostgreSQL releases it automatically if
the connection dies, so a crashed process cannot wedge the lock forever.

``pg_try_advisory_lock`` is used rather than ``pg_advisory_lock`` — a second sync
request should be told "one is already running", not silently queue behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.logging import get_logger

logger = get_logger(__name__)

# Arbitrary but fixed application-wide key. Must stay stable across releases:
# changing it would let an old and a new process sync simultaneously.
SYNC_LOCK_KEY = 8_472_301


class LockNotAcquiredError(Exception):
    """Another holder has the lock."""


@asynccontextmanager
async def advisory_lock(session: AsyncSession, key: int = SYNC_LOCK_KEY) -> AsyncIterator[None]:
    """Hold a session-scoped advisory lock, or raise LockNotAcquiredError immediately.

    The lock is tied to the *connection*, so the same session must be used for the
    unlock — hence the explicit release in the finally block rather than relying
    on transaction end.
    """
    acquired = (
        await session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
    ).scalar_one()

    if not acquired:
        logger.info("advisory_lock_busy", key=key)
        raise LockNotAcquiredError(f"Advisory lock {key} is held by another session.")

    logger.debug("advisory_lock_acquired", key=key)
    try:
        yield
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        logger.debug("advisory_lock_released", key=key)
