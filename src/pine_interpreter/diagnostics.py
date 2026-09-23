"""Source locations and interpreter exceptions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A one-based source position."""

    filename: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}:{self.column}"


class PineError(Exception):
    """Base class for user-facing interpreter errors."""

    def __init__(self, message: str, location: SourceLocation | None = None) -> None:
        self.message = message
        self.location = location
        super().__init__(message)

    def __str__(self) -> str:
        if self.location is None:
            return self.message
        return f"{self.location}: {self.message}"


class PineSyntaxError(PineError):
    """Raised when source text cannot be parsed."""


class PineRuntimeError(PineError):
    """Raised when a valid operation cannot be evaluated."""
