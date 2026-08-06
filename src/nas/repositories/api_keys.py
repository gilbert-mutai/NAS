"""SQLAlchemy implementation of ApiKeyRepository."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.errors import ConflictError
from nas.db.models import ApiKeyRow
from nas.domain.entities import ApiKey
from nas.repositories.protocols import NewApiKey


def to_entity(row: ApiKeyRow) -> ApiKey:
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        key_hash=row.key_hash,
        scopes=frozenset(row.scopes or ()),
        is_active=row.is_active,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        description=row.description,
    )


class SqlAlchemyApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        stmt = select(ApiKeyRow).where(ApiKeyRow.prefix == prefix)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_entity(row) if row else None

    async def list_all(self) -> tuple[ApiKey, ...]:
        stmt = select(ApiKeyRow).order_by(ApiKeyRow.name.asc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(to_entity(row) for row in rows)

    async def create(self, data: NewApiKey) -> ApiKey:
        row = ApiKeyRow(
            name=data.name.strip(),
            description=data.description,
            prefix=data.prefix,
            key_hash=data.key_hash,
            scopes=sorted(data.scopes),
            expires_at=data.expires_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                f"An API key named {data.name!r} already exists.",
                details={"field": "name"},
            ) from exc
        await self._session.refresh(row)
        return to_entity(row)

    async def mark_used(self, api_key_id: int, *, when: datetime) -> None:
        """Record that a key was used.

        Written as a targeted UPDATE rather than a load-modify-save so concurrent
        requests using the same key cannot deadlock or overwrite one another.
        """
        stmt = (
            update(ApiKeyRow)
            .where(ApiKeyRow.id == api_key_id)
            .values(last_used_at=when)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)
        # Commit immediately, and deliberately so.
        #
        # This UPDATE takes a row lock on the API key. Left to commit with the rest
        # of the request, that lock would be held for the request's entire
        # lifetime — and since every caller sharing a key touches the same row,
        # all requests using that key would serialise behind the slowest one. A
        # long POST /sync would stall every other ClientManager call. Committing here holds
        # the lock for microseconds instead.
        #
        # Safe because authentication runs before any endpoint work, so there is
        # nothing else pending on this session to commit prematurely.
        await self._session.commit()

    async def revoke(self, name: str) -> bool:
        stmt = (
            update(ApiKeyRow)
            .where(ApiKeyRow.name == name, ApiKeyRow.is_active.is_(True))
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        # An UPDATE always yields a CursorResult, which carries rowcount; the
        # generic Result type does not, hence the narrowing cast.
        return bool(cast("CursorResult[Any]", result).rowcount)
