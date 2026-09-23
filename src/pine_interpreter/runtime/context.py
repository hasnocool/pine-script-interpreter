"""Lexical scopes for script execution."""

from __future__ import annotations

from pine_interpreter.runtime.values import RuntimeValue


class Scope:
    """A nested mapping of variable names to runtime values."""

    def __init__(self, parent: Scope | None = None) -> None:
        self.parent = parent
        self._values: dict[str, RuntimeValue] = {}

    def child(self) -> Scope:
        return Scope(self)

    def declare(self, name: str, value: RuntimeValue) -> None:
        self._values[name] = value

    def assign(self, name: str, value: RuntimeValue) -> None:
        scope: Scope | None = self
        while scope is not None:
            if name in scope._values:
                scope._values[name] = value
                return
            scope = scope.parent
        self._values[name] = value

    def get(self, name: str) -> RuntimeValue:
        scope: Scope | None = self
        while scope is not None:
            if name in scope._values:
                return scope._values[name]
            scope = scope.parent
        raise KeyError(name)

    def local_values(self) -> dict[str, RuntimeValue]:
        return dict(self._values)
