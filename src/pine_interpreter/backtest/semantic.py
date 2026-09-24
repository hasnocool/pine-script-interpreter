"""Lightweight semantic analysis and feature inventory for Pine sources."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, Literal

from pine_interpreter.backtest.engine import BacktestEngine
from pine_interpreter.backtest.errors import source_digest
from pine_interpreter.backtest.registry import is_known_member, is_known_namespace
from pine_interpreter.diagnostics import PineError
from pine_interpreter.parser.ast_nodes import (
    BooleanLiteral,
    CallExpression,
    Identifier,
    MemberExpression,
    Node,
    NumberLiteral,
    WhileStatement,
)

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One source validation or approximation diagnostic."""

    severity: Severity
    code: str
    message: str
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticReport:
    """Parse result plus a feature inventory and non-fatal diagnostics."""

    source_hash: str
    features: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_hash": self.source_hash,
            "valid": self.valid,
            "features": list(self.features),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_source(source: str) -> SemanticReport:
    """Parse a source and collect high-level runtime feature usage."""

    digest = source_digest(source)
    try:
        program = BacktestEngine.validate_source(source)
    except PineError as exc:
        return SemanticReport(
            digest,
            (),
            (
                ValidationIssue(
                    "error",
                    "parse_error",
                    str(exc),
                    str(exc.location) if exc.location else None,
                ),
            ),
        )

    features: set[str] = set()
    issues: list[ValidationIssue] = []
    _check_duplicate_declarations(program, issues)
    _walk(program, features, issues)
    for feature in sorted(features):
        if feature.startswith("request."):
            issues.append(
                ValidationIssue(
                    "warning",
                    "approximate_request",
                    f"{feature} is evaluated with the current chart context",
                )
            )
    return SemanticReport(digest, tuple(sorted(features)), tuple(issues))


def _walk(node: Any, features: set[str], issues: list[ValidationIssue]) -> None:
    if isinstance(node, CallExpression):
        name = _call_name(node.callee)
        if name:
            features.add(name)
            _check_call(node, name, issues)
    if isinstance(node, MemberExpression):
        name = _member_name(node)
        if name:
            features.add(name)
            _check_member(node, name, issues)
    if isinstance(node, WhileStatement) and isinstance(node.condition, BooleanLiteral):
        if node.condition.value:
            issues.append(
                ValidationIssue(
                    "warning",
                    "potential_unbounded_loop",
                    "while true may require an execution-step or wall-clock limit",
                )
            )
    if isinstance(node, Node) or is_dataclass(node):
        for field in fields(node) if is_dataclass(node) else ():
            value = getattr(node, field.name)
            if isinstance(value, (tuple, list)):
                for child in value:
                    _walk(child, features, issues)
            elif is_dataclass(value):
                _walk(value, features, issues)


def _check_duplicate_declarations(program: Any, issues: list[ValidationIssue]) -> None:
    seen: dict[str, str] = {}
    for statement in getattr(program, "statements", ()):
        name = getattr(statement, "name", None)
        if not isinstance(name, str):
            continue
        kind = type(statement).__name__
        if name in seen:
            issues.append(
                ValidationIssue(
                    "error",
                    "duplicate_declaration",
                    f"{kind} redeclares {name!r}; the first declaration is {seen[name]}",
                    str(getattr(statement, "location", "")) or None,
                )
            )
        else:
            seen[name] = kind


def _check_member(node: Any, name: str, issues: list[ValidationIssue]) -> None:
    if "." not in name:
        return
    namespace, member = name.split(".", 1)
    if not is_known_namespace(namespace):
        issues.append(
            ValidationIssue(
                "warning",
                "unknown_namespace",
                f"unknown Pine namespace: {namespace}",
                str(getattr(node, "location", "")) or None,
            )
        )
    elif not is_known_member(namespace, member):
        issues.append(
            ValidationIssue(
                "warning",
                "unknown_member",
                f"unknown {namespace} member: {member}",
                str(getattr(node, "location", "")) or None,
            )
        )


def _check_call(node: CallExpression, name: str, issues: list[ValidationIssue]) -> None:
    _check_member(node, name, issues) if isinstance(node.callee, MemberExpression) else None
    if name.startswith("ta."):
        member = name[3:]
        if member in {
            "sma",
            "ema",
            "rma",
            "wma",
            "hma",
            "vwma",
            "swma",
            "alma",
            "rsi",
            "atr",
            "cci",
            "mfi",
            "wpr",
            "cmo",
            "stoch",
            "bbands",
            "kc",
            "donchian",
            "supertrend",
            "sar",
            "adx",
            "dmi",
            "aroon",
            "momentum",
            "roc",
            "change",
            "correlation",
            "cog",
            "linreg",
            "highest",
            "lowest",
            "highestbars",
            "lowestbars",
            "percentrank",
        }:
            length_indices: tuple[int, ...]
            if member == "macd":
                length_indices = (1, 2, 3)
            elif member == "stoch":
                length_indices = (3,)
            elif member == "sar":
                length_indices = (0, 1)
            else:
                length_indices = (1,)
            for index in length_indices:
                if index >= len(node.arguments):
                    continue
                argument = node.arguments[index]
                if isinstance(argument, NumberLiteral) and argument.value <= 0:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "invalid_length",
                            f"{name} requires a positive numeric length",
                            str(getattr(node, "location", "")) or None,
                        )
                    )
                    break
    if name in {"strategy.entry", "strategy.order"} and len(node.arguments) < 2:
        issues.append(
            ValidationIssue(
                "error",
                "invalid_order",
                f"{name} requires an id and direction",
                str(getattr(node, "location", "")) or None,
            )
        )


def _call_name(expression: Any) -> str | None:
    if isinstance(expression, Identifier):
        return expression.name
    if isinstance(expression, MemberExpression):
        object_name = _call_name(expression.object)
        return f"{object_name}.{expression.property}" if object_name else None
    return None


def _member_name(expression: MemberExpression) -> str | None:
    if isinstance(expression.object, Identifier):
        return f"{expression.object.name}.{expression.property}"
    return None
