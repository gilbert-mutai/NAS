"""Switch inventory use-cases."""

from __future__ import annotations

from dataclasses import dataclass

from nas.core.credentials import CredentialProvider, NullCredentialProvider
from nas.core.errors import SwitchNotFoundError
from nas.domain.entities import Switch
from nas.domain.enums import CredentialStatus
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import SwitchFilters, SwitchRepository


@dataclass(frozen=True, slots=True)
class SwitchView:
    """A switch plus derived, non-sensitive operational state.

    ``credential_status`` reports whether the switch's credential reference
    resolves. The secret itself is never read here and never leaves the service.
    """

    switch: Switch
    credential_status: CredentialStatus


class SwitchService:
    def __init__(
        self,
        *,
        repository: SwitchRepository,
        credential_provider: CredentialProvider,
    ) -> None:
        self._repository = repository
        self._credentials = credential_provider
        self._store_configured = not isinstance(credential_provider, NullCredentialProvider)

    async def list_switches(
        self, *, filters: SwitchFilters, page_request: PageRequest
    ) -> Page[SwitchView]:
        page = await self._repository.list(filters=filters, page_request=page_request)
        return Page(
            items=tuple(self._to_view(switch) for switch in page.items),
            total=page.total,
            page=page.page,
            page_size=page.page_size,
        )

    async def get_switch(self, switch_id: int) -> SwitchView:
        switch = await self._repository.get_by_id(switch_id)
        if switch is None:
            raise SwitchNotFoundError(
                f"No switch exists with id {switch_id}.",
                details={"switch_id": switch_id},
            )
        return self._to_view(switch)

    def _to_view(self, switch: Switch) -> SwitchView:
        return SwitchView(
            switch=switch,
            credential_status=switch.credential_status(
                store_configured=self._store_configured,
                resolvable=self._credentials.has(switch.credential_ref),
            ),
        )
