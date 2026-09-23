"""Historical scalar storage used by the future time-series runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class Series(Generic[T]):
    """Values indexed by absolute bar number."""

    _values: list[T] = field(default_factory=list, init=False)

    def append(self, value: T) -> None:
        self._values.append(value)

    def __getitem__(self, bar_index: int) -> T:
        return self._values[bar_index]

    @property
    def values(self) -> tuple[T, ...]:
        return tuple(self._values)
