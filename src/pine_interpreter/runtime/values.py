"""Runtime value types."""

from __future__ import annotations

from typing import TypeAlias

Scalar: TypeAlias = bool | int | float | str
RuntimeValue: TypeAlias = Scalar | None
