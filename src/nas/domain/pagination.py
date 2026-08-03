"""Pagination primitives shared by services and the API layer."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A validated pagination request.

    ``page`` is 1-indexed. The page size is clamped rather than rejected so a
    caller asking for more than the ceiling still gets a useful response.
    """

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", max(1, self.page))
        object.__setattr__(self, "page_size", min(max(1, self.page_size), MAX_PAGE_SIZE))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus the totals needed to render a pager."""

    items: tuple[T, ...]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return -(-self.total // self.page_size)  # ceiling division

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_previous(self) -> bool:
        return self.page > 1
