"""A small, deterministic Pine runtime for OHLCV backtests.

The existing project runtime intentionally covers only a basic expression
subset.  This module adds a separate execution path for common Pine
strategies: declarations, series references, moving averages, crossover
signals, and ``strategy.entry``/``strategy.close`` orders.  Unsupported
plotting and nonessential Pine built-ins are accepted and ignored so a
strategy can be validated without implementing the entire TradingView API.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pine_interpreter.backtest.models import (
    BACKTEST_RUNTIME_VERSION,
    BacktestConfig,
    BacktestExecutionLimitError,
    BacktestExecutionTimeoutError,
    BacktestReport,
    BacktestValidationError,
    Candle,
    EquityPoint,
    Trade,
)
from pine_interpreter.backtest.registry import (
    NAMESPACE_MEMBERS,
    constant_value,
    is_known_member,
)
from pine_interpreter.lexer import Lexer
from pine_interpreter.parser import Parser
from pine_interpreter.parser.ast_nodes import (
    ArrayLiteral,
    AssignmentStatement,
    BinaryExpression,
    BooleanLiteral,
    BraceLiteral,
    BreakStatement,
    CallArgument,
    CallExpression,
    ColorLiteral,
    ConditionalExpression,
    ContinueStatement,
    EnumDeclaration,
    Expression,
    ExpressionStatement,
    ForExpression,
    ForStatement,
    FunctionDeclaration,
    HistoryExpression,
    Identifier,
    IfExpression,
    IfStatement,
    ImportDeclaration,
    MemberExpression,
    NaLiteral,
    Node,
    NumberLiteral,
    Program,
    Statement,
    StringLiteral,
    SwitchCase,
    SwitchExpression,
    SwitchStatement,
    TupleDeclaration,
    TypeDeclaration,
    UnaryExpression,
    VariableDeclaration,
    WhileStatement,
)

_COLOR_NAMES = {
    "aqua",
    "black",
    "blue",
    "fuchsia",
    "gray",
    "green",
    "grey",
    "lime",
    "maroon",
    "navy",
    "olive",
    "orange",
    "purple",
    "red",
    "silver",
    "teal",
    "white",
    "yellow",
}
_NAMESPACE_NAMES = set(NAMESPACE_MEMBERS) | {
    "array",
    "barmerge",
    "color",
    "currency",
    "display",
    "format",
    "input",
    "location",
    "math",
    "plot",
    "request",
    "shape",
    "size",
    "strategy",
    "syminfo",
    "ta",
    "timeframe",
    "barstate",
    "box",
    "hline",
    "label",
    "line",
    "matrix",
    "map",
    "position",
    "table",
}
_TYPE_NAMES = {
    "bool",
    "const",
    "float",
    "input",
    "int",
    "integer",
    "line",
    "series",
    "simple",
    "string",
    "resolution",
    "source",
    "timeframe",
}

_MISSING = object()


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


@dataclass(slots=True)
class _OpenPosition:
    side: str
    quantity: float
    entry_price: float
    entry_index: int
    entry_timestamp: datetime
    entry_fee: float
    entries_count: int = 1


class _Broker:
    """Small market-order broker with close-price fills."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.cash = config.initial_cash
        self.pyramiding_limit = config.pyramiding
        self.position: _OpenPosition | None = None
        self.trades: list[Trade] = []

    def _fill_price(self, price: float, side: str) -> float:
        slippage = self.config.slippage_bps / 10_000
        half_spread = self.config.spread_bps / 20_000
        return price * (
            1 + half_spread + slippage if side == "long" else 1 - half_spread - slippage
        )

    def enter(
        self,
        side: str,
        quantity: float,
        price: float,
        index: int,
        timestamp: datetime,
        reason: str = "entry",
    ) -> None:
        if quantity <= 0 or not math.isfinite(quantity):
            raise BacktestValidationError("order quantity must be positive and finite")
        if side == "short" and not self.config.allow_short:
            raise BacktestValidationError("short entries are disabled by configuration")
        if self.position is not None:
            if self.position.side == side:
                if self.position.entries_count >= self.pyramiding_limit:
                    return
                fill = self._fill_price(price, side)
                fee = abs(quantity * fill) * self.config.fee_rate + self.config.commission_per_trade
                cash_delta = quantity * fill + fee if side == "long" else -quantity * fill + fee
                if self.cash - cash_delta < -1e-9:
                    raise BacktestValidationError("order exceeds available cash")
                total_quantity = self.position.quantity + quantity
                self.position = _OpenPosition(
                    side,
                    total_quantity,
                    (self.position.entry_price * self.position.quantity + fill * quantity)
                    / total_quantity,
                    self.position.entry_index,
                    self.position.entry_timestamp,
                    self.position.entry_fee + fee,
                    self.position.entries_count + 1,
                )
                self.cash -= cash_delta
                return
            self.close(price, index, timestamp, reason="reverse")
        fill = self._fill_price(price, side)
        fee = abs(quantity * fill) * self.config.fee_rate + self.config.commission_per_trade
        if side == "long":
            self.cash -= quantity * fill + fee
        else:
            self.cash += quantity * fill - fee
        if self.cash < -1e-9:
            raise BacktestValidationError("order exceeds available cash")
        self.position = _OpenPosition(side, quantity, fill, index, timestamp, fee)

    def partial_close(
        self,
        price: float,
        index: int,
        timestamp: datetime,
        *,
        quantity: float | None = None,
        reason: str = "exit",
    ) -> Trade | None:
        """Close all or part of the open position at a modeled fill."""

        if self.position is None:
            return None
        position = self.position
        requested = position.quantity if quantity is None else self._positive_quantity(quantity)
        if requested > position.quantity + 1e-9:
            raise BacktestValidationError("partial exit quantity exceeds the open position")
        if requested <= 0:
            return None
        fraction = requested / position.quantity
        fill = self._fill_price(price, position.side)
        exit_fee = requested * abs(fill) * self.config.fee_rate + self.config.commission_per_trade
        allocated_entry_fee = position.entry_fee * fraction
        if position.side == "long":
            gross_pnl = requested * (fill - position.entry_price)
            self.cash += requested * fill - exit_fee
        else:
            gross_pnl = requested * (position.entry_price - fill)
            self.cash -= requested * fill + exit_fee
        pnl = gross_pnl - allocated_entry_fee - exit_fee
        trade = Trade(
            position.entry_index,
            index,
            position.entry_timestamp,
            timestamp,
            position.side,
            requested,
            position.entry_price,
            fill,
            pnl,
            allocated_entry_fee + exit_fee,
            reason,
        )
        self.trades.append(trade)
        if requested >= position.quantity - 1e-9:
            self.position = None
        else:
            self.position = _OpenPosition(
                position.side,
                position.quantity - requested,
                position.entry_price,
                position.entry_index,
                position.entry_timestamp,
                position.entry_fee - allocated_entry_fee,
                position.entries_count,
            )
        return trade

    @staticmethod
    def _positive_quantity(quantity: float) -> float:
        if not math.isfinite(quantity) or quantity <= 0:
            raise BacktestValidationError("order quantity must be positive and finite")
        return quantity

    def close(
        self,
        price: float,
        index: int,
        timestamp: datetime,
        reason: str = "exit",
    ) -> Trade | None:
        return self.partial_close(price, index, timestamp, quantity=None, reason=reason)

    def close_all(self, price: float, index: int, timestamp: datetime) -> Trade | None:
        return self.close(price, index, timestamp, reason="close_all")

    def equity(self, price: float) -> float:
        if self.position is None:
            return self.cash
        if self.position.side == "long":
            unrealized = self.position.quantity * (price - self.position.entry_price)
        else:
            unrealized = self.position.quantity * (self.position.entry_price - price)
        return self.cash + unrealized

    def unrealized(self, price: float) -> float:
        if self.position is None:
            return 0.0
        if self.position.side == "long":
            return self.position.quantity * (price - self.position.entry_price)
        return self.position.quantity * (self.position.entry_price - price)

    def apply_funding(self, price: float) -> float:
        """Apply a configurable per-bar funding approximation."""

        if self.position is None or self.config.funding_rate_bps == 0:
            return 0.0
        notional = self.position.quantity * abs(price)
        payment = notional * self.config.funding_rate_bps / 10_000
        if self.position.side == "long":
            self.cash -= payment
        else:
            self.cash += payment
        return payment


@dataclass(slots=True)
class _PendingExit:
    stop: float | None
    limit: float | None
    from_entry: str | None
    quantity: float | None = None


@dataclass(slots=True)
class _PendingEntry:
    order_id: str
    side: str
    quantity: float
    stop: float | None
    limit: float | None
    placed_index: int


class _PineRuntime:
    """Evaluate one parsed Pine program against normalized candles."""

    def __init__(
        self,
        program: Program,
        config: BacktestConfig,
        input_overrides: Mapping[str, Any] | None = None,
        library_root: str | Path | None = None,
    ) -> None:
        self.program = program
        self.config = config
        self.broker = _Broker(config)
        self.initial_cash = config.initial_cash
        self.strategy_default_qty_type = "fixed"
        self.strategy_default_qty_value = config.default_qty
        self.values: dict[str, Any] = {}
        self.persistent: dict[str, Any] = {}
        self.persistent_names: set[str] = set()
        self.history: dict[str, list[Any]] = {}
        self.call_history: dict[str, list[Any]] = {}
        self._equity_history: list[float] = []
        self.call_seen: set[str] = set()
        self.input_cache: dict[str, Any] = {}
        self.input_overrides = dict(input_overrides or {})
        self.approximations: set[str] = set()
        self.library_root = Path(library_root).resolve() if library_root is not None else None
        self.libraries: dict[str, Program] = {}
        self.library_dependencies: set[str] = set()
        self._library_imports: dict[int, dict[str, Program]] = {}
        self._function_owner: dict[int, Program] = {}
        self._library_stack: tuple[Path, ...] = ()
        self._active_library: Program | None = None
        self.functions: dict[str, FunctionDeclaration] = {}
        self.enums: dict[str, dict[str, Any]] = {}
        self.current_candle: Candle | None = None
        self.current_index = -1
        self.symbol = "UNKNOWN"
        self.timeframe = "unknown"
        self.exchange = "synthetic"
        self.execution_steps = 0
        self.execution_started_at: float | None = None
        self.pending_exits: dict[str, _PendingExit] = {}
        self.pending_entries: list[_PendingEntry] = []
        self._register_declarations()

    def _register_declarations(self) -> None:
        for statement in self.program.statements:
            if isinstance(statement, ImportDeclaration):
                self._load_import(statement, self.program)
        for statement in self.program.statements:
            if isinstance(statement, FunctionDeclaration):
                self.functions[statement.name] = statement
                self._function_owner[id(statement)] = self.program
            elif isinstance(statement, EnumDeclaration):
                self.enums[statement.name] = self._enum_values(statement)

    def _load_import(self, declaration: ImportDeclaration, owner: Program) -> None:
        if self.library_root is None:
            raise BacktestValidationError("Pine library imports require a configured library_root")
        import_name = declaration.import_path.strip().strip('"').strip("'")
        self.library_dependencies.add(import_name)
        if not import_name or Path(import_name).is_absolute() or ".." in Path(import_name).parts:
            raise BacktestValidationError(f"invalid Pine library import path: {import_name!r}")
        candidates = [
            self.library_root / f"{import_name}.pine",
            self.library_root / import_name,
        ]
        source_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source_path is None:
            # Archive library names can include a publisher suffix such as
            # ``Name__hash``.  Match the stem exactly before giving up.
            requested_stem = Path(import_name).name.casefold()
            matches = sorted(
                candidate
                for candidate in self.library_root.rglob("*.pine")
                if candidate.stem.casefold() == requested_stem
            )
            source_path = matches[0] if matches else None
        if source_path is None:
            raise BacktestValidationError(f"imported Pine library was not found: {import_name}")
        try:
            resolved = source_path.resolve()
            resolved.relative_to(self.library_root)
        except ValueError as exc:
            raise BacktestValidationError(
                "imported Pine library is outside the library root"
            ) from exc
        if resolved in self._library_stack:
            chain = " -> ".join(path.name for path in (*self._library_stack, resolved))
            raise BacktestValidationError(f"circular Pine library import: {chain}")
        source = resolved.read_text(encoding="utf-8")
        self._library_stack = (*self._library_stack, resolved)
        try:
            library_program = _parse_source_cached(source)
            alias = declaration.alias or resolved.stem
            self.libraries[alias] = library_program
            self._library_imports.setdefault(id(owner), {})[alias] = library_program
            for statement in library_program.statements:
                if isinstance(statement, FunctionDeclaration):
                    self._function_owner[id(statement)] = library_program
                elif isinstance(statement, ImportDeclaration):
                    self._load_import(statement, library_program)
        finally:
            self._library_stack = self._library_stack[:-1]

    @staticmethod
    def _enum_values(declaration: EnumDeclaration) -> dict[str, Any]:
        result: dict[str, Any] = {}
        next_value: int | str = 0
        for member in declaration.members:
            raw_value = member.value
            if isinstance(raw_value, (NumberLiteral, StringLiteral, BooleanLiteral)):
                value = raw_value.value
            elif raw_value is None:
                value = next_value
            else:
                value = member.name
            result[member.name] = value
            if isinstance(value, int):
                next_value = value + 1
        return result

    def run(
        self,
        candles: Sequence[Candle],
        *,
        name: str,
        symbol: str,
        timeframe: str,
        exchange: str,
    ) -> BacktestReport:
        equity_curve: list[EquityPoint] = []
        peak = self.initial_cash
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange
        self.execution_started_at = time.perf_counter()
        for index, candle in enumerate(candles):
            self._check_pending_entries(candle, index)
            self._check_pending_exits(candle, index)
            self.current_candle = candle
            self.current_index = index
            self.call_seen.clear()
            self.values = {
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "time": int(candle.timestamp.timestamp()),
                "timenow": candle.timestamp,
                "bar_index": index,
                "last_bar_index": len(candles) - 1,
                "na": None,
            }
            self.values.update(self.persistent)
            for statement in self.program.statements:
                self._execute_statement(statement, self.values)
            if self.config.funding_rate_bps:
                self.broker.apply_funding(candle.close)
                self.approximations.add("funding.per_bar")
            equity = self.broker.equity(candle.close)
            self._equity_history.append(equity)
            peak = max(peak, equity)
            equity_curve.append(EquityPoint(index, candle.timestamp, equity, peak - equity))
            self._record_history()
        if candles and self.config.close_at_end:
            last = candles[-1]
            self.broker.close_all(last.close, len(candles) - 1, last.timestamp)
            equity = self.broker.cash
            if equity_curve:
                point = equity_curve[-1]
                equity_curve[-1] = EquityPoint(
                    point.index, point.timestamp, equity, max(0.0, peak - equity)
                )
                if self._equity_history:
                    self._equity_history[-1] = equity
        return BacktestReport(
            name,
            symbol,
            timeframe,
            exchange,
            self.initial_cash,
            self.broker.equity(candles[-1].close) if candles else self.initial_cash,
            tuple(self.broker.trades),
            tuple(equity_curve),
            len(candles),
            self.execution_steps,
            tuple(sorted(self.approximations)),
            BACKTEST_RUNTIME_VERSION,
            self.config,
            tuple(sorted(self.library_dependencies)),
        )

    def _check_pending_entries(self, candle: Candle, index: int) -> None:
        if not self.pending_entries:
            return
        remaining: list[_PendingEntry] = []
        for order in self.pending_entries:
            if order.placed_index >= index:
                remaining.append(order)
                continue
            stop_hit = order.stop is not None and (
                candle.high >= order.stop if order.side == "long" else candle.low <= order.stop
            )
            limit_hit = order.limit is not None and (
                candle.low <= order.limit if order.side == "long" else candle.high >= order.limit
            )
            triggered = False
            fill_price: float | None = None
            open_stop = order.stop is not None and (
                candle.open >= order.stop if order.side == "long" else candle.open <= order.stop
            )
            open_limit = order.limit is not None and (
                candle.open <= order.limit if order.side == "long" else candle.open >= order.limit
            )
            if self.config.intrabar_policy == "open_first" and (open_stop or open_limit):
                fill_price = candle.open
                triggered = True
            elif open_stop:
                fill_price = candle.open
                triggered = True
            elif open_limit:
                fill_price = candle.open
                triggered = True
            elif stop_hit and limit_hit:
                if self.config.intrabar_policy == "limit_first":
                    fill_price = order.limit
                else:
                    fill_price = order.stop
                triggered = True
            elif stop_hit:
                fill_price = order.stop
                triggered = True
            elif limit_hit:
                fill_price = order.limit
                triggered = True
            if triggered and fill_price is not None:
                self.broker.enter(
                    order.side,
                    order.quantity,
                    fill_price,
                    index,
                    candle.timestamp,
                    reason="entry",
                )
            else:
                remaining.append(order)
        self.pending_entries = remaining

    def _check_pending_exits(self, candle: Candle, index: int) -> None:
        if self.broker.position is None:
            self.pending_exits.clear()
            return
        position = self.broker.position
        pending = self.pending_exits.get(position.side)
        if pending is None:
            return
        stop_hit = pending.stop is not None and (
            candle.low <= pending.stop if position.side == "long" else candle.high >= pending.stop
        )
        limit_hit = pending.limit is not None and (
            candle.high >= pending.limit if position.side == "long" else candle.low <= pending.limit
        )
        open_stop = pending.stop is not None and (
            candle.open <= pending.stop if position.side == "long" else candle.open >= pending.stop
        )
        open_limit = pending.limit is not None and (
            candle.open >= pending.limit
            if position.side == "long"
            else candle.open <= pending.limit
        )
        fill_price: float | None = None
        reason: str | None = None
        if self.config.intrabar_policy == "open_first" and (open_stop or open_limit):
            fill_price, reason = candle.open, "open"
        elif open_stop:
            fill_price, reason = candle.open, "stop"
        elif open_limit:
            fill_price, reason = candle.open, "limit"
        elif stop_hit and limit_hit:
            if self.config.intrabar_policy == "limit_first":
                fill_price, reason = pending.limit, "limit"
            else:
                fill_price, reason = pending.stop, "stop"
        elif stop_hit:
            fill_price, reason = pending.stop, "stop"
        elif limit_hit:
            fill_price, reason = pending.limit, "limit"
        if fill_price is not None and reason is not None:
            self.broker.partial_close(
                fill_price,
                index,
                candle.timestamp,
                quantity=pending.quantity,
                reason=reason,
            )
            self.pending_exits.pop(position.side, None)

    def _record_history(self) -> None:
        names = set(self.history) | set(self.values)
        for name in names:
            self.history.setdefault(name, []).append(self.values.get(name))
        for key, values in self.call_history.items():
            if key not in self.call_seen:
                values.append(None)

    def _consume_execution_step(self) -> None:
        self.execution_steps += 1
        if self.execution_steps > self.config.max_execution_steps:
            raise BacktestExecutionLimitError(
                f"execution step limit exceeded ({self.config.max_execution_steps})"
            )
        if (
            self.execution_started_at is not None
            and time.perf_counter() - self.execution_started_at > self.config.max_execution_seconds
        ):
            raise BacktestExecutionTimeoutError(
                f"execution time limit exceeded ({self.config.max_execution_seconds:g}s)"
            )

    def _execute_statements(self, statements: Sequence[Statement], values: dict[str, Any]) -> Any:
        result: Any = None
        for statement in statements:
            result = self._execute_statement(statement, values)
        return result

    def _execute_statement(self, statement: Statement, values: dict[str, Any]) -> Any:
        try:
            return self._execute_statement_inner(statement, values)
        except BacktestValidationError as exc:
            if getattr(exc, "location", None) is None:
                location = getattr(statement, "location", None)
                if location is not None:
                    exc.location = str(location)
            raise

    def _execute_statement_inner(self, statement: Statement, values: dict[str, Any]) -> Any:
        self._consume_execution_step()
        if isinstance(statement, VariableDeclaration):
            if statement.storage in {"var", "varip"} or statement.qualifier == "input":
                if statement.name in self.persistent:
                    values[statement.name] = self.persistent[statement.name]
                    return values[statement.name]
                value = self._evaluate(statement.value, values)
                values[statement.name] = value
                self.persistent[statement.name] = value
                self.persistent_names.add(statement.name)
                return value
            value = self._evaluate(statement.value, values)
            values[statement.name] = value
            self.history.setdefault(statement.name, [])
            return value
        if isinstance(statement, AssignmentStatement):
            value = self._evaluate(statement.value, values)
            self._assign(statement.target, value, values)
            return value
        if isinstance(statement, TupleDeclaration):
            value = self._evaluate(statement.value, values)
            if not isinstance(value, (list, tuple)) or len(value) != len(statement.targets):
                if value is None:
                    for target in statement.targets:
                        self._assign(target, None, values)
                    return None
                raise BacktestValidationError("tuple assignment expects a matching sequence")
            for target, item in zip(statement.targets, value, strict=True):
                self._assign(target, item, values)
            return value
        if isinstance(statement, ExpressionStatement):
            if (
                isinstance(statement.expression, Identifier)
                and statement.expression.name == "break"
            ):
                raise _Break
            if (
                isinstance(statement.expression, Identifier)
                and statement.expression.name == "continue"
            ):
                raise _Continue
            return self._evaluate(statement.expression, values)
        if isinstance(statement, IfStatement):
            branch = (
                statement.then_branch
                if self._truthy(self._evaluate(statement.condition, values))
                else statement.else_branch
            )
            return self._execute_statements(branch, values)
        if isinstance(statement, ForStatement):
            return self._execute_for(
                statement.variable,
                statement.start,
                statement.end,
                statement.step,
                statement.body,
                values,
            )
        if isinstance(statement, WhileStatement):
            while self._truthy(self._evaluate(statement.condition, values)):
                self._consume_execution_step()
                try:
                    self._execute_statements(statement.body, values)
                except _Break:
                    break
                except _Continue:
                    continue
            return None
        if isinstance(statement, BreakStatement):
            raise _Break
        if isinstance(statement, ContinueStatement):
            raise _Continue
        if isinstance(statement, SwitchStatement):
            return self._execute_switch(statement.subject, statement.cases, values)
        if isinstance(statement, FunctionDeclaration):
            self.functions[statement.name] = statement
            return None
        if isinstance(statement, (TypeDeclaration, EnumDeclaration, ImportDeclaration)):
            return None
        raise BacktestValidationError(f"unsupported Pine statement: {type(statement).__name__}")

    def _execute_for(
        self,
        variable: str | tuple[Expression, ...],
        start_expression: Expression,
        end_expression: Expression | None,
        step_expression: Expression | None,
        body: Sequence[Statement],
        values: dict[str, Any],
    ) -> Any:
        start = int(self._number(self._evaluate(start_expression, values)))
        end = int(self._number(self._evaluate(end_expression, values))) if end_expression else None
        step = int(self._number(self._evaluate(step_expression, values))) if step_expression else 1
        if step == 0:
            raise BacktestValidationError("for-loop step cannot be zero")
        result: Any = None
        current = start
        while end is None or (current < end if step > 0 else current > end):
            self._consume_execution_step()
            if isinstance(variable, str):
                values[variable] = current
            else:
                for target, item in zip(variable, (current,), strict=False):
                    self._assign(target, item, values)
            try:
                result = self._execute_statements(body, values)
            except _Break:
                break
            except _Continue:
                pass
            current += step
        return result

    def _execute_switch(
        self,
        subject_expression: Expression | None,
        cases: Sequence[SwitchCase],
        values: dict[str, Any],
    ) -> Any:
        subject = self._evaluate(subject_expression, values) if subject_expression else None
        for case in cases:
            if not case.patterns or any(
                self._equal(self._evaluate(pattern, values), subject) for pattern in case.patterns
            ):
                return self._execute_statements(case.body, values)
        return None

    def _assign(self, target: Expression, value: Any, values: dict[str, Any]) -> None:
        if isinstance(target, Identifier):
            values[target.name] = value
            if target.name in self.persistent_names:
                self.persistent[target.name] = value
            return
        if isinstance(target, MemberExpression):
            object_value = self._evaluate(target.object, values)
            if isinstance(object_value, dict):
                object_value[target.property] = value
            return
        raise BacktestValidationError("assignment target must be an identifier or member")

    def _evaluate(self, expression: Expression, values: dict[str, Any]) -> Any:
        if isinstance(expression, NumberLiteral):
            return expression.value
        if isinstance(expression, StringLiteral):
            return expression.value
        if isinstance(expression, BooleanLiteral):
            return expression.value
        if isinstance(expression, ColorLiteral):
            return expression.value
        if isinstance(expression, NaLiteral):
            return None
        if isinstance(expression, Identifier):
            if expression.name in values:
                return values[expression.name]
            if expression.name in _COLOR_NAMES:
                return expression.name
            if expression.name in _NAMESPACE_NAMES:
                return expression.name
            if expression.name in _TYPE_NAMES:
                return expression.name
            if expression.name == "tickerid":
                return "UNKNOWN"
            if expression.name == "dayofweek":
                return self._time_call("weekday", ())
            if expression.name == "tr":
                high = self._number(values.get("high"))
                low = self._number(values.get("low"))
                previous = self.history.get("close", [])
                previous_close = (
                    self._number(previous[-1]) if previous else self._number(values.get("close"))
                )
                return max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            if expression.name in {"hl2", "hlc3", "ohlc4"}:
                high = self._number(values.get("high"))
                low = self._number(values.get("low"))
                close = self._number(values.get("close"))
                open_value = self._number(values.get("open"))
                if expression.name == "hl2":
                    return (high + low) / 2
                if expression.name == "hlc3":
                    return (high + low + close) / 3
                return (open_value + high + low + close) / 4
            if expression.name in {"hour", "year", "month", "dayofmonth", "minute", "second"}:
                return self._time_call(expression.name, ())
            if expression.name in self.enums:
                return expression.name
            if expression.name in self.functions:
                return self._call_function(self.functions[expression.name], (), {}, values)
            raise BacktestValidationError(f"unknown Pine identifier: {expression.name}")
        if isinstance(expression, UnaryExpression):
            return self._unary(expression, values)
        if isinstance(expression, BinaryExpression):
            return self._binary(expression, values)
        if isinstance(expression, ConditionalExpression):
            branch = (
                expression.when_true
                if self._truthy(self._evaluate(expression.condition, values))
                else expression.when_false
            )
            return self._evaluate(branch, values)
        if isinstance(expression, MemberExpression):
            return self._member(expression, values)
        if isinstance(expression, HistoryExpression):
            offset_expression = self._evaluate(expression.offset, values)
            return self._history(
                expression.expression, int(self._number(offset_expression)), values
            )
        if isinstance(expression, CallExpression):
            return self._call(expression, values)
        if isinstance(expression, ArrayLiteral):
            return [self._evaluate(element, values) for element in expression.elements]
        if isinstance(expression, BraceLiteral):
            return {str(self._evaluate(element, values)): True for element in expression.elements}
        if isinstance(expression, IfExpression):
            if_branch = (
                expression.then_branch
                if self._truthy(self._evaluate(expression.condition, values))
                else expression.else_branch
            )
            return self._execute_statements(if_branch, values)
        if isinstance(expression, SwitchExpression):
            return self._execute_switch(expression.subject, expression.cases, values)
        if isinstance(expression, ForExpression):
            return self._execute_for(
                expression.variable,
                expression.start,
                expression.end,
                expression.step,
                expression.body,
                values,
            )
        raise BacktestValidationError(f"unsupported Pine expression: {type(expression).__name__}")

    def _unary(self, expression: UnaryExpression, values: dict[str, Any]) -> Any:
        value = self._evaluate(expression.operand, values)
        if value is None:
            return None
        if expression.operator == "+":
            return self._number(value)
        if expression.operator == "-":
            return -self._number(value)
        if expression.operator == "not":
            return not self._truthy(value)
        raise BacktestValidationError(f"unsupported unary operator: {expression.operator}")

    def _binary(self, expression: BinaryExpression, values: dict[str, Any]) -> Any:
        left = self._evaluate(expression.left, values)
        if expression.operator == "and":
            return self._truthy(left) and self._truthy(self._evaluate(expression.right, values))
        if expression.operator == "or":
            return self._truthy(left) or self._truthy(self._evaluate(expression.right, values))
        right = self._evaluate(expression.right, values)
        if expression.operator == "==":
            return self._equal(left, right)
        if expression.operator == "!=":
            return not self._equal(left, right)
        if left is None or right is None:
            return None
        if expression.operator in {"<", "<=", ">", ">="}:
            left_number = self._number(left)
            right_number = self._number(right)
            return {
                "<": left_number < right_number,
                "<=": left_number <= right_number,
                ">": left_number > right_number,
                ">=": left_number >= right_number,
            }[expression.operator]
        if expression.operator == "+" and isinstance(left, str) and isinstance(right, str):
            return left + right
        left_number = self._number(left)
        right_number = self._number(right)
        if expression.operator == "+":
            return left_number + right_number
        if expression.operator == "-":
            return left_number - right_number
        if expression.operator == "*":
            return left_number * right_number
        if expression.operator == "/":
            return left_number / right_number if right_number else None
        if expression.operator == "%":
            return left_number % right_number if right_number else None
        if expression.operator == "**":
            try:
                return left_number**right_number
            except (OverflowError, ValueError) as exc:
                raise BacktestValidationError("numeric result is out of range") from exc
        raise BacktestValidationError(f"unsupported binary operator: {expression.operator}")

    def _member(self, expression: MemberExpression, values: dict[str, Any]) -> Any:
        if isinstance(expression.object, Identifier):
            name = expression.object.name
            if name in self.enums:
                enum = self.enums[name]
                return enum.get(expression.property, expression.property)
            if name == "strategy":
                return self._strategy_member(expression.property)
            if name == "math":
                value = constant_value(name, expression.property)
                return math.pi if expression.property == "pi" else value
            if name == "timeframe":
                if expression.property == "period":
                    return self.timeframe
                if expression.property == "multiplier":
                    try:
                        return int(self.timeframe.rstrip("mhdwS") or 1)
                    except ValueError:
                        return 1
                if expression.property == "isdaily":
                    return self.timeframe.lower().endswith("d")
                if expression.property == "isweekly":
                    return self.timeframe.lower().endswith("w")
                if expression.property == "ismonthly":
                    return "M" in self.timeframe
                if expression.property == "isintraday":
                    return not self.timeframe.lower().endswith(("d", "w", "M"))
                if expression.property == "isminutes":
                    return self.timeframe.lower().endswith("m")
                if expression.property == "isseconds":
                    return self.timeframe.lower().endswith("s")
                if expression.property == "in_seconds":
                    return (
                        int(self.timeframe[:-1]) * 60 if self.timeframe.lower().endswith("m") else 0
                    )
            if name == "syminfo":
                dynamic = {
                    "tickerid": self.symbol,
                    "ticker": self.symbol,
                    "currency": "USD",
                }
                if expression.property in dynamic:
                    return dynamic[expression.property]
            if name == "barstate":
                return {
                    "isconfirmed": True,
                    "isfirst": self.current_index == 0,
                    "islast": self.current_index == self.values.get("last_bar_index", -1),
                    "ishistory": True,
                    "isrealtime": False,
                    "islastconfirmedhistory": self.current_index
                    == self.values.get("last_bar_index", -1),
                }.get(expression.property)
            if name == "session":
                return expression.property
            if name == "dayofweek":
                return expression.property
            if name == "source":
                return "close"
            if name == "resolution":
                return "1h"
            if name in NAMESPACE_MEMBERS:
                value = constant_value(name, expression.property)
                return expression.property if value is None else value
        object_value = self._evaluate(expression.object, values)
        if isinstance(object_value, dict):
            return object_value.get(expression.property)
        return None

    def _strategy_member(self, name: str) -> Any:
        price = self.current_candle.close if self.current_candle else 0.0
        position = self.broker.position
        if name in {"long", "longposition"}:
            return "long"
        if name in {"short", "shortposition"}:
            return "short"
        if name in {"percent_of_equity", "cash", "fixed", "contracts"}:
            return name
        if name == "flat":
            return "flat"
        if name == "position_size":
            if position is None:
                return 0.0
            return position.quantity if position.side == "long" else -position.quantity
        if name == "position_avg_price":
            return position.entry_price if position is not None else 0.0
        if name == "initial_capital":
            return self.initial_cash
        if name == "opentrades":
            return 1 if self.broker.position is not None else 0
        if name == "closedtrades":
            return len(self.broker.trades)
        if name == "equity":
            return self.broker.equity(price)
        if name == "netprofit":
            return self.broker.equity(price) - self.initial_cash
        if name == "openprofit":
            return self.broker.unrealized(price)
        if name == "grossprofit":
            return sum(trade.pnl for trade in self.broker.trades if trade.pnl > 0)
        if name == "grossloss":
            return -sum(trade.pnl for trade in self.broker.trades if trade.pnl < 0)
        if name == "wintrades":
            return sum(trade.pnl > 0 for trade in self.broker.trades)
        if name == "losstrades":
            return sum(trade.pnl < 0 for trade in self.broker.trades)
        if name == "max_drawdown":
            peak = self.initial_cash
            drawdown = 0.0
            for point in self._equity_history:
                peak = max(peak, point)
                drawdown = max(drawdown, peak - point)
            return drawdown
        if name == "max_drawdown_percent":
            return self._strategy_member("max_drawdown") / self.initial_cash * 100
        if name in {"max_runup", "max_runup_percent"}:
            return 0.0
        if name == "risk":
            return None
        if is_known_member("strategy", name):
            return None
        raise BacktestValidationError(f"unknown strategy member: {name}")

    def _call(self, expression: CallExpression, values: dict[str, Any]) -> Any:
        arguments: list[Any] = []
        argument_nodes: list[Expression] = []
        keywords: dict[str, Any] = {}
        for argument in expression.arguments:
            argument_node = argument.value if isinstance(argument, CallArgument) else argument
            argument_nodes.append(argument_node)
            if isinstance(argument, CallArgument):
                keywords[argument.name or ""] = self._evaluate(argument.value, values)
            else:
                arguments.append(self._evaluate(argument, values))
        callee = self._callee_name(expression.callee)
        if callee is None:
            raise BacktestValidationError("unsupported Pine call target")
        result = self._call_named(callee, arguments, argument_nodes, keywords, values, expression)
        key = self._series_key(expression)
        if key is not None and key.startswith("call:") and key not in self.call_seen:
            self.call_history.setdefault(key, []).append(result)
            self.call_seen.add(key)
        return result

    def _lookup_function(self, name: str) -> FunctionDeclaration | None:
        if self._active_library is not None and "." not in name:
            imported = next(
                (
                    statement
                    for statement in self._active_library.statements
                    if isinstance(statement, FunctionDeclaration) and statement.name == name
                ),
                None,
            )
            if imported is not None:
                return imported
        return self.functions.get(name)

    def _call_named(
        self,
        name: str,
        arguments: list[Any],
        argument_nodes: Sequence[Expression],
        keywords: dict[str, Any],
        values: dict[str, Any],
        node: Node,
    ) -> Any:
        if name == "strategy":
            initial_cash = keywords.get("initial_capital")
            if initial_cash is not None:
                self.initial_cash = self._number(initial_cash)
                if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
                    raise BacktestValidationError("strategy initial_capital must be positive")
                self.broker.cash = self.initial_cash
            default_qty_type = keywords.get("default_qty_type")
            if default_qty_type is not None:
                self.strategy_default_qty_type = str(default_qty_type)
            default_qty_value = keywords.get("default_qty_value")
            if default_qty_value is not None:
                self.strategy_default_qty_value = self._number(default_qty_value)
            pyramiding = keywords.get("pyramiding")
            if pyramiding is not None:
                self.broker.pyramiding_limit = max(1, int(self._number(pyramiding)))
            return None
        if name in {
            "indicator",
            "strategy",
            "plot",
            "plotshape",
            "plotchar",
            "plotcandle",
            "hline",
            "bgcolor",
            "barcolor",
            "fill",
            "alert",
            "alertcondition",
            "library",
            "import",
        }:
            return None
        if name in {"input", "float", "int", "bool", "string"}:
            if not arguments:
                return None
            value = arguments[0]
            if value is None or name == "input":
                return value
            if name == "float":
                return self._number(value)
            if name == "int":
                return int(self._number(value))
            if name == "bool":
                return self._truthy(value)
            return str(value)
        if name == "iff":
            return (
                arguments[1]
                if len(arguments) > 1 and self._truthy(arguments[0])
                else (arguments[2] if len(arguments) > 2 else None)
            )
        if name in {
            "sma",
            "ema",
            "rma",
            "wma",
            "hma",
            "vwma",
            "rsi",
            "atr",
            "tr",
            "cci",
            "mfi",
            "wpr",
            "cmo",
            "macd",
            "stoch",
            "bbands",
            "kc",
            "donchian",
            "supertrend",
            "sar",
            "adx",
            "dmi",
            "aroon",
            "highest",
            "lowest",
            "highestbars",
            "lowestbars",
            "change",
            "roc",
            "momentum",
            "valuewhen",
            "barssince",
            "cum",
            "vwap",
            "crossover",
            "crossunder",
            "cross",
            "pivotlow",
            "pivothigh",
            "linreg",
            "stdev",
        }:
            return self._ta_call(
                "crossover" if name == "cross" else name, arguments, argument_nodes, values, node
            )
        if name == "sum":
            return self._math_call("sum", arguments)
        if name == "timeframe.in_seconds":
            value = self.timeframe.lower()
            if value.endswith("m"):
                return int(value[:-1]) * 60
            if value.endswith("h"):
                return int(value[:-1]) * 3_600
            if value.endswith("d"):
                return int(value[:-1]) * 86_400
            return 0
        if name == "security" and len(arguments) >= 3:
            self.approximations.add("request.security")
            return arguments[2]
        if name.startswith(("line.", "label.", "box.", "table.", "ticker.", "draw.")):
            return None
        if name == "na":
            return arguments[0] if arguments else None
        if name == "nz":
            return (
                arguments[0]
                if arguments and arguments[0] is not None
                else (arguments[1] if len(arguments) > 1 else 0)
            )
        if name == "fixnan":
            return (
                arguments[0]
                if arguments and arguments[0] is not None
                else (arguments[1] if len(arguments) > 1 else 0)
            )
        if name in {"abs", "sign"} and len(arguments) == 1:
            value = arguments[0]
            return (
                None
                if value is None
                else (
                    abs(self._number(value))
                    if name == "abs"
                    else (0 if self._number(value) == 0 else (1 if self._number(value) > 0 else -1))
                )
            )
        if name.startswith("math."):
            return self._math_call(name[5:], arguments)
        if name.startswith("ta."):
            return self._ta_call(name[3:], arguments, argument_nodes, values, node)
        if name.startswith("strategy.closedtrades."):
            suffix = name[len("strategy.closedtrades.") :]
            index = int(self._number(arguments[0])) if arguments else 0
            if index < 0 or index >= len(self.broker.trades):
                return None
            trade = self.broker.trades[index]
            return {
                "profit": trade.pnl,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "entry_time": int(trade.entry_timestamp.timestamp()),
                "exit_time": int(trade.exit_timestamp.timestamp()),
                "entry_bar_index": trade.entry_index,
                "exit_bar_index": trade.exit_index,
                "quantity": trade.quantity,
                "side": trade.side,
            }.get(suffix)
        if name.startswith("strategy.opentrades."):
            suffix = name[len("strategy.opentrades.") :]
            position = self.broker.position
            if position is None:
                return None
            return {
                "size": position.quantity,
                "entry_price": position.entry_price,
                "entry_time": int(position.entry_timestamp.timestamp()),
                "entry_bar_index": position.entry_index,
            }.get(suffix)
        if name in {"strategy.risk.allow_entry_in", "strategy.risk.max_intraday_filled_orders"}:
            return True if name.endswith("allow_entry_in") else self.config.pyramiding
        if name == "strategy.opentrades":
            return self.broker.position
        if name == "strategy.closedtrades":
            return len(self.broker.trades)
        if name.startswith("strategy."):
            return self._strategy_call(name[9:], arguments, keywords, node)
        if name.startswith("input."):
            title = keywords.get("title", arguments[1] if len(arguments) > 1 else None)
            if title is not None and str(title) in self.input_overrides:
                return self.input_overrides[str(title)]
            key = f"{name}:{arguments!r}:{keywords!r}"
            if key not in self.input_cache:
                self.input_cache[key] = arguments[0] if arguments else None
            return self.input_cache[key]
        if name.startswith("array."):
            return self._array_call(name[6:], arguments)
        if name.startswith("str."):
            return self._string_call(name[4:], arguments)
        if name.startswith("color."):
            return name[6:]
        if name in {"request.security", "request.security_lower_tf"} and len(arguments) >= 3:
            self.approximations.add(name)
            return arguments[2]
        if name == "request.security_syminfo":
            self.approximations.add(name)
            return arguments[0] if arguments else "UNKNOWN"
        if name in {
            "request.financial",
            "request.dividends",
            "request.earnings",
            "request.quandl",
        }:
            self.approximations.add(name)
            return 0.0
        if name in {
            "timestamp",
            "time",
            "time_open",
            "time_close",
            "year",
            "month",
            "dayofmonth",
            "hour",
            "minute",
            "second",
            "weekday",
        }:
            return self._time_call(name, arguments)
        if name == "abs":
            return self._math_call("abs", arguments)
        if name in {"max", "min", "round", "floor", "ceil", "sqrt", "pow", "avg"}:
            return self._math_call(name, arguments)
        if "." in name:
            target_name, method_name = name.split(".", 1)
            if target_name in values:
                dynamic = self._object_method(values[target_name], method_name, arguments)
                if dynamic is not _MISSING:
                    return dynamic
        function = self._lookup_function(name)
        if function is not None:
            return self._call_function(function, arguments, keywords, values)
        if "." in name:
            namespace, function_name = name.split(".", 1)
            library = self._library_imports.get(id(self._active_library), {}).get(namespace)
            if library is None:
                library = self.libraries.get(namespace)
            if library is not None:
                imported = next(
                    (
                        statement
                        for statement in library.statements
                        if isinstance(statement, FunctionDeclaration)
                        and statement.name == function_name
                    ),
                    None,
                )
                if imported is not None:
                    return self._call_function(imported, arguments, keywords, values)
        raise BacktestValidationError(f"unknown Pine builtin: {name}")

    def _math_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if not arguments or arguments[0] is None and name not in {"sum"}:
            return None
        numbers = [self._number(value) for value in arguments if value is not None]
        if name == "max":
            return max(numbers)
        if name == "min":
            return min(numbers)
        if name == "avg":
            return sum(numbers) / len(numbers)
        if name == "sum":
            return sum(numbers)
        if not numbers:
            return None
        if name == "round":
            digits = int(self._number(arguments[1])) if len(arguments) > 1 else 0
            return round(numbers[0], digits)
        if name == "floor":
            return math.floor(numbers[0])
        if name == "ceil":
            return math.ceil(numbers[0])
        if name == "sqrt":
            return math.sqrt(numbers[0]) if numbers[0] >= 0 else None
        if name == "pow" and len(numbers) > 1:
            try:
                return numbers[0] ** numbers[1]
            except (OverflowError, ValueError) as exc:
                raise BacktestValidationError("numeric result is out of range") from exc
        return numbers[0]

    def _object_method(self, target: Any, name: str, arguments: Sequence[Any]) -> Any:
        if isinstance(target, list):
            if target and isinstance(target[0], list) and name in {"get", "set"}:
                if name == "get" and len(arguments) >= 2:
                    row = int(self._number(arguments[0]))
                    column = int(self._number(arguments[1]))
                    return (
                        target[row][column]
                        if 0 <= row < len(target) and 0 <= column < len(target[row])
                        else None
                    )
                if name == "set" and len(arguments) >= 3:
                    row = int(self._number(arguments[0]))
                    column = int(self._number(arguments[1]))
                    if 0 <= row < len(target) and 0 <= column < len(target[row]):
                        target[row][column] = arguments[2]
                    return None
            if name == "rows" and target and isinstance(target[0], list):
                return len(target)
            if name == "columns" and target and isinstance(target[0], list):
                return len(target[0])
            if name in {"push", "append"} and arguments:
                target.append(arguments[0])
                return target
            if name in {"pop", "shift"}:
                return target.pop() if target else None
            if name == "unshift" and arguments:
                target.insert(0, arguments[0])
                return target
            if name == "get" and arguments:
                index = int(self._number(arguments[0]))
                return target[index] if -len(target) <= index < len(target) else None
            if name == "set" and len(arguments) >= 2:
                index = int(self._number(arguments[0]))
                if -len(target) <= index < len(target):
                    target[index] = arguments[1]
                return None
            if name == "size":
                return len(target)
            if name == "clear":
                target.clear()
                return target
            if name == "includes" and arguments:
                return arguments[0] in target
            if name == "indexof" and arguments:
                return target.index(arguments[0]) if arguments[0] in target else -1
            if name == "lastindexof" and arguments:
                return (
                    len(target) - 1 - target[::-1].index(arguments[0])
                    if arguments[0] in target
                    else -1
                )
            if name in {"sum", "avg", "min", "max", "abs", "median", "mode", "stdev", "variance"}:
                numbers = [self._number(value) for value in target if value is not None]
                if not numbers:
                    return None
                if name == "sum":
                    return sum(numbers)
                if name == "avg":
                    return sum(numbers) / len(numbers)
                if name == "min":
                    return min(numbers)
                if name == "max":
                    return max(numbers)
                if name == "abs":
                    return [abs(value) for value in numbers]
                if name == "median":
                    ordered = sorted(numbers)
                    middle = len(ordered) // 2
                    return (
                        ordered[middle]
                        if len(ordered) % 2
                        else (ordered[middle - 1] + ordered[middle]) / 2
                    )
                if name == "mode":
                    return max(set(numbers), key=numbers.count)
                average = sum(numbers) / len(numbers)
                if name == "variance":
                    return sum((value - average) ** 2 for value in numbers) / len(numbers)
                return math.sqrt(sum((value - average) ** 2 for value in numbers) / len(numbers))
            if name == "reverse":
                target.reverse()
                return target
            if name == "sort":
                target.sort(key=lambda value: self._number(value))
                return target
            if name == "copy":
                return list(target)
            if name == "fill" and arguments:
                target[:] = [arguments[0]] * len(target)
                return target
        if isinstance(target, dict):
            if name == "get" and arguments:
                return target.get(self._string_key(arguments[0]))
            if name == "put" and len(arguments) >= 2:
                target[self._string_key(arguments[0])] = arguments[1]
                return arguments[1]
            if name == "contains_key" and arguments:
                return self._string_key(arguments[0]) in target
            if name == "remove" and arguments:
                return target.pop(self._string_key(arguments[0]), None)
            if name == "keys":
                return list(target)
            if name == "values":
                return list(target.values())
            if name == "size":
                return len(target)
            if name == "clear":
                target.clear()
                return target
        if isinstance(target, str):
            if name == "length":
                return len(target)
            if name == "upper":
                return target.upper()
            if name == "lower":
                return target.lower()
            if name == "contains" and arguments:
                return str(arguments[0]) in target
            if name == "replace" and len(arguments) >= 2:
                return target.replace(str(arguments[0]), str(arguments[1]))
        return _MISSING

    @staticmethod
    def _string_key(value: Any) -> str:
        return str(value)

    def _array_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if name in {"new", "new_float", "new_int", "new_string", "new_bool"}:
            size = int(self._number(arguments[0])) if arguments else 0
            value = arguments[1] if len(arguments) > 1 else (0.0 if name != "new_string" else "")
            return [value] * max(0, size)
        if name == "size" and arguments:
            return len(arguments[0]) if isinstance(arguments[0], (list, tuple, str, dict)) else None
        if name == "get" and len(arguments) > 1:
            target = arguments[0]
            index = int(self._number(arguments[1]))
            return (
                target[index]
                if isinstance(target, list) and -len(target) <= index < len(target)
                else None
            )
        if name == "push" and len(arguments) > 1 and isinstance(arguments[0], list):
            arguments[0].append(arguments[1])
            return arguments[0]
        if name == "pop" and arguments and isinstance(arguments[0], list):
            return arguments[0].pop() if arguments[0] else None
        if name == "shift" and arguments and isinstance(arguments[0], list):
            return arguments[0].pop(0) if arguments[0] else None
        if name == "unshift" and len(arguments) > 1 and isinstance(arguments[0], list):
            arguments[0].insert(0, arguments[1])
            return arguments[0]
        return None

    def _string_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if not arguments or arguments[0] is None:
            return None
        value = str(arguments[0])
        if name == "tostring":
            return value
        if name == "length":
            return len(value)
        if name == "upper":
            return value.upper()
        if name == "lower":
            return value.lower()
        if name == "contains" and len(arguments) > 1:
            return str(arguments[1]) in value
        if name == "replace" and len(arguments) > 2:
            return value.replace(str(arguments[1]), str(arguments[2]))
        if name == "split" and len(arguments) > 1:
            separator = str(arguments[1])
            return list(value) if not separator else value.split(separator)
        return None

    def _ohlcv_series(self, name: str) -> list[Any]:
        if self.current_candle is None:
            return []
        current = getattr(self.current_candle, name)
        return [*self.history.get(name, []), current]

    def _true_range_series(self) -> list[float]:
        highs = self._ohlcv_series("high")
        lows = self._ohlcv_series("low")
        closes = self._ohlcv_series("close")
        ranges: list[float] = []
        for index in range(len(closes)):
            high = self._number(highs[index])
            low = self._number(lows[index])
            previous_close = self._number(closes[index - 1]) if index else high
            ranges.append(
                max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            )
        return ranges

    def _ema_series(self, values: Sequence[Any], length: int) -> list[float]:
        numbers = [self._number(value) for value in values if value is not None]
        if not numbers:
            return []
        alpha = 2 / (length + 1)
        result = [numbers[0]]
        for number in numbers[1:]:
            result.append(alpha * number + (1 - alpha) * result[-1])
        return result

    def _last_number(self, values: Sequence[Any]) -> float | None:
        for value in reversed(values):
            if value is not None:
                return self._number(value)
        return None

    def _ta_call(
        self,
        name: str,
        arguments: Sequence[Any],
        argument_nodes: Sequence[Expression],
        values: dict[str, Any],
        node: Node,
    ) -> Any:
        if name in {"crossover", "crossunder"} and len(arguments) > 1:
            current_left = self._number(arguments[0]) if arguments[0] is not None else None
            current_right = self._number(arguments[1]) if arguments[1] is not None else None
            previous_left = self._previous_number(argument_nodes[0], values)
            previous_right = self._previous_number(argument_nodes[1], values)
            if (
                current_left is None
                or current_right is None
                or previous_left is None
                or previous_right is None
            ):
                return False
            if name == "crossover":
                return current_left > current_right and previous_left <= previous_right
            return current_left < current_right and previous_left >= previous_right
        if name == "tr":
            ranges = self._true_range_series()
            return ranges[-1] if ranges else None
        if name == "atr":
            length = int(self._number(arguments[0])) if arguments else 14
            if length <= 0:
                raise BacktestValidationError("ta.atr length must be positive")
            ranges = self._true_range_series()
            return self._moving_average("rma", ranges, length)
        if name == "rsi":
            if len(arguments) < 2:
                return None
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError("ta.rsi length must be positive")
            series = [
                value
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            numbers = [self._number(value) for value in series]
            if len(numbers) <= length:
                return None
            changes = [numbers[index] - numbers[index - 1] for index in range(1, len(numbers))]
            window = changes[-length:]
            gains = sum(max(change, 0) for change in window) / length
            losses = sum(max(-change, 0) for change in window) / length
            if losses == 0:
                return 100.0
            relative_strength = gains / losses
            return 100 - (100 / (1 + relative_strength))
        if name == "cci" and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError("ta.cci length must be positive")
            series = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            window = series[-length:]
            if len(window) < length:
                return None
            average = sum(window) / length
            deviation = sum(abs(value - average) for value in window) / length
            return (window[-1] - average) / (0.015 * deviation) if deviation else 0.0
        if name == "mfi" and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError("ta.mfi length must be positive")
            highs = self._ohlcv_series("high")[-length:]
            lows = self._ohlcv_series("low")[-length:]
            closes = self._ohlcv_series("close")[-length:]
            volumes = self._ohlcv_series("volume")[-length:]
            typical_prices = [
                (self._number(high) + self._number(low) + self._number(close)) / 3
                for high, low, close in zip(highs, lows, closes, strict=False)
            ]
            if len(typical_prices) <= length:
                return None
            positive = 0.0
            negative = 0.0
            for index in range(1, len(typical_prices)):
                flow = typical_prices[index] * self._number(volumes[index])
                if typical_prices[index] > typical_prices[index - 1]:
                    positive += flow
                elif typical_prices[index] < typical_prices[index - 1]:
                    negative += flow
            return 100 - (100 / (1 + positive / negative)) if negative else 100.0
        if name in {"wpr", "cmo"}:
            length = int(self._number(arguments[0])) if arguments else 14
            if length <= 0:
                raise BacktestValidationError(f"ta.{name} length must be positive")
            if name == "wpr":
                highs = [self._number(value) for value in self._ohlcv_series("high")[-length:]]
                lows = [self._number(value) for value in self._ohlcv_series("low")[-length:]]
                closes = self._ohlcv_series("close")
                close = self._last_number(closes)
                if not highs or not lows or close is None or max(highs) == min(lows):
                    return None
                return -100 * (max(highs) - close) / (max(highs) - min(lows))
            closes = [self._number(value) for value in self._ohlcv_series("close")]
            if len(closes) <= length:
                return None
            changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
            window = changes[-length:]
            upward = sum(change for change in window if change > 0)
            downward = -sum(change for change in window if change < 0)
            return (upward - downward) / (upward + downward) * 100 if upward + downward else 0.0
        if name == "barssince" and arguments:
            series = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            for offset, value in enumerate(reversed(series)):
                if self._truthy(value):
                    return offset
            return None
        if name == "cum" and arguments:
            series = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            total = 0.0
            for value in series:
                if value is not None:
                    total += self._number(value)
            return total
        if name == "vwap":
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            volumes = self._ohlcv_series("volume")
            numerator = 0.0
            denominator = 0.0
            for high, low, close, volume in zip(highs, lows, closes, volumes, strict=False):
                if None in (high, low, close, volume):
                    continue
                typical = (self._number(high) + self._number(low) + self._number(close)) / 3
                numerator += typical * self._number(volume)
                denominator += self._number(volume)
            return numerator / denominator if denominator else None
        if name == "stoch" and len(arguments) >= 4:
            length = int(self._number(arguments[3]))
            if length <= 0:
                raise BacktestValidationError("ta.stoch length must be positive")
            source_series = [
                value
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            high_series = self._ohlcv_series("high")
            low_series = self._ohlcv_series("low")
            if len(source_series) < length or len(high_series) < length:
                return [None, None]
            highest = max(self._number(value) for value in high_series[-length:])
            lowest = min(self._number(value) for value in low_series[-length:])
            current = self._number(source_series[-1])
            percent_k = (current - lowest) / (highest - lowest) * 100 if highest != lowest else 50.0
            return [percent_k, percent_k]
        if name in {"midpoint", "midprice"} and arguments:
            series = [
                value
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            length = int(self._number(arguments[1])) if len(arguments) > 1 else len(series)
            if length <= 0:
                raise BacktestValidationError("ta.length must be positive")
            window = [self._number(value) for value in series[-length:]]
            if not window:
                return None
            return (max(window) + min(window)) / 2
        if name == "range" and len(arguments) >= 2:
            high = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            low = self._series_values_for_value(
                arguments[1], values, argument_nodes[1] if len(argument_nodes) > 1 else None
            )
            pairs = [
                (self._number(high_value), self._number(low_value))
                for high_value, low_value in zip(high, low, strict=False)
                if high_value is not None and low_value is not None
            ]
            return max((high_value - low_value for high_value, low_value in pairs), default=0.0)
        if name in {"highestbars", "lowestbars"} and len(arguments) >= 2:
            series = [
                value
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError(f"ta.{name} length must be positive")
            window = series[-length:]
            if not window:
                return None
            target = max(window) if name == "highestbars" else min(window)
            return len(window) - 1 - window[::-1].index(target)
        if name == "momentum" and len(arguments) >= 2:
            series = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            length = int(self._number(arguments[1]))
            if length <= 0 or len(series) <= length:
                return None
            current = series[-1]
            previous = series[-length - 1]
            if current is None or previous is None:
                return None
            return self._number(current) - self._number(previous)
        if name == "linreg" and len(arguments) >= 2:
            series = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            length = int(self._number(arguments[1]))
            if length <= 0 or len(series) < length:
                return None
            window = series[-length:]
            x_mean = (length - 1) / 2
            y_mean = sum(window) / length
            denominator = sum((index - x_mean) ** 2 for index in range(length))
            slope = (
                sum((index - x_mean) * (value - y_mean) for index, value in enumerate(window))
                / denominator
                if denominator
                else 0.0
            )
            return [y_mean - slope * x_mean, slope]
        if name == "cog" and len(arguments) >= 2:
            series = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError("ta.cog length must be positive")
            window = series[-length:]
            denominator = length * (length + 1) / 2
            return sum((index + 1) * value for index, value in enumerate(window)) / denominator
        if name == "aroon" and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError("ta.aroon length must be positive")
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            if len(highs) <= length or len(lows) <= length:
                return [None, None]
            high_window = list(range(len(highs) - length - 1, len(highs)))
            low_window = list(range(len(lows) - length - 1, len(lows)))
            high_index = max(high_window, key=lambda index: self._number(highs[index]))
            low_index = max(low_window, key=lambda index: self._number(lows[index]))
            return [low_index - high_index, high_index - low_index]
        if name in {"adx", "plusdi", "minusdi"} and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                raise BacktestValidationError(f"ta.{name} length must be positive")
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            if len(closes) <= length + 1:
                return None
            plus_moves: list[float] = []
            minus_moves: list[float] = []
            true_ranges: list[float] = []
            for index in range(1, len(closes)):
                up = self._number(highs[index]) - self._number(highs[index - 1])
                down = self._number(lows[index - 1]) - self._number(lows[index])
                plus_moves.append(max(up, 0.0) if up > down else 0.0)
                minus_moves.append(max(down, 0.0) if down > up else 0.0)
                true_ranges.append(self._true_range_series()[index])
            tr_average = self._moving_average("rma", true_ranges, length) or 0.0
            plus_average = self._moving_average("rma", plus_moves, length) or 0.0
            minus_average = self._moving_average("rma", minus_moves, length) or 0.0
            plus_di = 100 * plus_average / tr_average if tr_average else 0.0
            minus_di = 100 * minus_average / tr_average if tr_average else 0.0
            if name == "plusdi":
                return plus_di
            if name == "minusdi":
                return minus_di
            return (
                100 * abs(plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.0
            )
        if name == "sar" and len(arguments) >= 2:
            self.approximations.add("ta.sar")
            step = self._number(arguments[0])
            maximum = self._number(arguments[1])
            if step <= 0 or maximum <= 0:
                raise BacktestValidationError("ta.sar parameters must be positive")
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            if len(highs) < 2:
                return None
            rising = self._number(highs[-1]) >= self._number(highs[-2])
            extreme = max(self._number(value) for value in (highs[-2:] if rising else lows[-2:]))
            previous = self._number(lows[-2] if rising else highs[-2])
            return (
                previous + (extreme - previous) + step * (extreme - previous)
                if rising
                else previous - (previous - extreme) - step * (previous - extreme)
            )
        if not arguments:
            return None
        source = arguments[0]
        source_node = argument_nodes[0] if argument_nodes else None
        length = (
            int(self._number(arguments[1]))
            if len(arguments) > 1 and arguments[1] is not None and name != "anchored_vwap"
            else 1
        )
        if length <= 0:
            raise BacktestValidationError("ta.length must be positive")
        if name == "obv":
            closes = self._ohlcv_series("close")
            volumes = self._ohlcv_series("volume")
            total = 0.0
            for index in range(1, len(closes)):
                close_delta = self._number(closes[index]) - self._number(closes[index - 1])
                total += self._number(volumes[index]) * (
                    1 if close_delta > 0 else -1 if close_delta < 0 else 0
                )
            return total
        if name == "anchored_vwap":
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            volumes = self._ohlcv_series("volume")
            start = arguments[1] if len(arguments) > 1 else 0
            start_index = 0
            if isinstance(start, (int, float)):
                start_index = max(0, int(start))
            numerator = 0.0
            denominator = 0.0
            for high, low, close, volume in zip(
                highs[start_index:],
                lows[start_index:],
                closes[start_index:],
                volumes[start_index:],
                strict=False,
            ):
                if None in (high, low, close, volume):
                    continue
                typical = (self._number(high) + self._number(low) + self._number(close)) / 3
                numerator += typical * self._number(volume)
                denominator += self._number(volume)
            return numerator / denominator if denominator else None
        if name in {"bb", "bbands"}:
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            if len(series) < length:
                return [None, None, None]
            middle = sum(series[-length:]) / length
            deviation = math.sqrt(sum((value - middle) ** 2 for value in series[-length:]) / length)
            multiplier = self._number(arguments[2]) if len(arguments) > 2 else 2.0
            return [middle, middle + multiplier * deviation, middle - multiplier * deviation]
        if name in {"pivothigh", "pivotlow"} and len(arguments) >= 3:
            left_length = int(self._number(arguments[1]))
            right_length = int(self._number(arguments[2]))
            if left_length <= 0 or right_length <= 0:
                raise BacktestValidationError("pivot lengths must be positive")
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            center = len(series) - right_length - 1
            if center < left_length or center >= len(series):
                return None
            window = series[center - left_length : center + right_length + 1]
            target = max(window) if name == "pivothigh" else min(window)
            return series[center] if series[center] == target else None
        if name in {"stdev", "variance", "mad"} and len(arguments) >= 2:
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ][-length:]
            if len(series) < length:
                return None
            average = sum(series) / length
            if name == "variance":
                return sum((value - average) ** 2 for value in series) / length
            if name == "mad":
                return sum(abs(value - average) for value in series) / length
            return math.sqrt(sum((value - average) ** 2 for value in series) / length)
        if name == "correlation" and len(arguments) >= 3:
            length = int(self._number(arguments[2]))
            left = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ][-length:]
            right = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[1], values, argument_nodes[1] if argument_nodes else None
                )
                if value is not None
            ][-length:]
            if len(left) < length or len(right) < length:
                return None
            left_mean = sum(left) / length
            right_mean = sum(right) / length
            numerator = sum(
                (left_value - left_mean) * (right_value - right_mean)
                for left_value, right_value in zip(left, right, strict=True)
            )
            denominator = math.sqrt(
                sum((value - left_mean) ** 2 for value in left)
                * sum((value - right_mean) ** 2 for value in right)
            )
            return numerator / denominator if denominator else 0.0
        if name == "vwma":
            series = self._series_values_for_value(source, values, source_node)
            volumes = self._ohlcv_series("volume")[-len(series) :]
            weighted = [
                self._number(value) * self._number(volume)
                for value, volume in zip(series, volumes, strict=False)
                if value is not None
            ]
            total_volume = sum(
                self._number(volume)
                for value, volume in zip(series, volumes, strict=False)
                if value is not None
            )
            return sum(weighted) / total_volume if total_volume else None
        if name == "swma":
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            if not series:
                return None
            weights = [
                1 / (abs((2 * index) - (len(series) - 1)) + 1) for index in range(len(series))
            ]
            return sum(value * weight for value, weight in zip(series, weights, strict=True)) / sum(
                weights
            )
        if name == "alma":
            offset = int(self._number(arguments[2])) if len(arguments) > 2 else int(length * 0.85)
            sigma = self._number(arguments[3]) if len(arguments) > 3 else 6.0
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ][-length:]
            if not series or sigma <= 0:
                return None
            size = len(series)
            weights = [
                math.exp(-((index - (size - 1 - offset)) ** 2) / (2 * sigma**2))
                for index in range(size)
            ]
            return sum(value * weight for value, weight in zip(series, weights, strict=True)) / sum(
                weights
            )
        if name in {"bbands", "kc", "donchian"}:
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            if len(series) < length:
                return [None, None, None]
            if name == "donchian":
                window = series[-length:]
                upper = max(window)
                lower = min(window)
                return [(upper + lower) / 2, upper, lower]
            middle = sum(series[-length:]) / length
            if name == "bbands":
                deviation = math.sqrt(
                    sum((value - middle) ** 2 for value in series[-length:]) / length
                )
                multiplier = self._number(arguments[2]) if len(arguments) > 2 else 2.0
                return [middle, middle + multiplier * deviation, middle - multiplier * deviation]
            ranges = self._true_range_series()
            average_range = self._moving_average("rma", ranges, length)
            multiplier = self._number(arguments[2]) if len(arguments) > 2 else 1.5
            if average_range is None:
                return [middle, None, None]
            return [
                middle,
                middle + multiplier * average_range,
                middle - multiplier * average_range,
            ]
        if name == "macd" and len(arguments) >= 3:
            fast_length = int(self._number(arguments[1]))
            slow_length = int(self._number(arguments[2]))
            signal_length = int(self._number(arguments[3])) if len(arguments) > 3 else 9
            if min(fast_length, slow_length, signal_length) <= 0:
                raise BacktestValidationError("ta.macd lengths must be positive")
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            fast = self._ema_series(series, fast_length)
            slow = self._ema_series(series, slow_length)
            macd_series = [
                fast_value - slow_value for fast_value, slow_value in zip(fast, slow, strict=False)
            ]
            signal = self._ema_series(macd_series, signal_length)
            macd_value = macd_series[-1] if macd_series else None
            signal_value = signal[-1] if signal else None
            return [
                macd_value,
                signal_value,
                macd_value - signal_value
                if macd_value is not None and signal_value is not None
                else None,
            ]
        if name == "supertrend" and len(arguments) >= 2:
            factor = self._number(arguments[0])
            atr_length = int(self._number(arguments[1]))
            if atr_length <= 0:
                raise BacktestValidationError("ta.supertrend length must be positive")
            ranges = self._true_range_series()
            average_range = self._moving_average("rma", ranges, atr_length)
            close = self._last_number(self._ohlcv_series("close"))
            if average_range is None or close is None:
                return [None, None]
            line = close - factor * average_range
            return [line, 1 if close >= line else -1]
        if name in {"sma", "ema", "rma", "wma", "hma"}:
            series = self._series_values_for_value(source, values, source_node)
            return self._moving_average(name, series, length)
        if name in {"highest", "lowest"}:
            series = [
                value
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            window = series[-length:]
            return (max(window) if name == "highest" else min(window)) if window else None
        if name == "change":
            series = self._series_values_for_value(source, values, source_node)
            if len(series) < 2 or series[-1] is None or series[-2] is None:
                return None
            change = self._number(series[-1]) - self._number(series[-2])
            return change
        if name == "roc":
            series = self._series_values_for_value(source, values, source_node)
            distance = int(self._number(arguments[1])) if len(arguments) > 1 else 1
            if distance <= 0 or len(series) <= distance:
                return None
            current = series[-1]
            previous = series[-distance - 1]
            if current is None or previous in (None, 0):
                return None
            return (self._number(current) / self._number(previous) - 1) * 100
        if name == "valuewhen" and len(arguments) > 2:
            source_values = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            condition_values = self._series_values_for_value(
                arguments[1], values, argument_nodes[1] if len(argument_nodes) > 1 else None
            )
            occurrence = int(self._number(arguments[2]))
            hits = [
                value
                for value, condition in zip(source_values, condition_values, strict=False)
                if self._truthy(condition)
            ]
            return hits[-occurrence - 1] if len(hits) > occurrence else None
        if name == "percentrank" and len(arguments) > 1:
            series = [
                self._number(value)
                for value in self._series_values_for_value(
                    arguments[0], values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            current_value: float | None = series[-1] if series else None
            return (
                sum(value <= current_value for value in series) / len(series) * 100
                if series and current_value is not None
                else None
            )
        return None

    def _time_call(self, name: str, arguments: Sequence[Any]) -> Any:
        timestamp = (
            arguments[0]
            if arguments
            else self.current_candle.timestamp
            if self.current_candle
            else datetime.now(UTC)
        )
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(self._number(timestamp), UTC)
        if not isinstance(timestamp, datetime):
            return None
        if name in {"timestamp", "time", "time_open", "time_close"}:
            return int(timestamp.timestamp())
        if name == "year":
            return timestamp.year
        if name == "month":
            return timestamp.month
        if name == "dayofmonth":
            return timestamp.day
        if name == "hour":
            return timestamp.hour
        if name == "minute":
            return timestamp.minute
        if name == "second":
            return timestamp.second
        if name == "weekday":
            return timestamp.strftime("%A").lower()
        return None

    def _queue_entry(
        self,
        order_id: str,
        side: str,
        quantity: float,
        stop: float | None,
        limit: float | None,
    ) -> None:
        if stop is not None and limit is not None:
            if side == "long" and stop >= limit:
                raise BacktestValidationError("long stop must be below limit")
            if side == "short" and stop <= limit:
                raise BacktestValidationError("short stop must be above limit")
        self.pending_entries = [
            order for order in self.pending_entries if order.order_id != order_id
        ]
        self.pending_entries.append(
            _PendingEntry(order_id, side, quantity, stop, limit, self.current_index)
        )

    def _entry_quantity(self, arguments: list[Any], keywords: dict[str, Any]) -> float:
        if self.current_candle is None:
            return self.config.default_qty
        raw_quantity = keywords.get("qty", arguments[2] if len(arguments) > 2 else None)
        if raw_quantity is not None:
            return self._number(raw_quantity)
        percent = keywords.get("qty_percent")
        if percent is not None:
            return max(
                0.0, self.broker.cash * self._number(percent) / 100 / self.current_candle.close
            )
        quantity_type = keywords.get("default_qty_type", self.strategy_default_qty_type)
        quantity_value = keywords.get("default_qty_value", self.strategy_default_qty_value)
        if quantity_type in {"percent_of_equity", "cash"}:
            if quantity_type == "percent_of_equity":
                return (
                    self.broker.cash
                    * self._number(quantity_value)
                    / 100
                    / self.current_candle.close
                )
            return self._number(quantity_value) / self.current_candle.close
        return self._number(quantity_value)

    def _exit_quantity(
        self,
        arguments: list[Any],
        keywords: dict[str, Any],
        position_quantity: float,
        quantity_index: int | None,
        percent_index: int | None,
    ) -> float | None:
        raw_quantity = keywords.get("qty")
        if raw_quantity is None and quantity_index is not None and len(arguments) > quantity_index:
            raw_quantity = arguments[quantity_index]
        if raw_quantity is not None:
            quantity = self._number(raw_quantity)
            if quantity <= 0:
                raise BacktestValidationError("exit quantity must be positive")
            return min(position_quantity, quantity)
        raw_percent = keywords.get("qty_percent")
        if raw_percent is None and percent_index is not None and len(arguments) > percent_index:
            raw_percent = arguments[percent_index]
        if raw_percent is not None:
            percent = self._number(raw_percent)
            if percent < 0 or percent > 100:
                raise BacktestValidationError("exit qty_percent must be in [0, 100]")
            return position_quantity * percent / 100
        return None

    def _strategy_call(
        self, name: str, arguments: list[Any], keywords: dict[str, Any], node: Node
    ) -> Any:
        if self.current_candle is None:
            return None
        if name in {"entry", "order"}:
            if not self._truthy(keywords.get("when", True)):
                return None
            direction = arguments[1] if len(arguments) > 1 else keywords.get("direction", "long")
            side = self._side(direction)
            quantity = self._entry_quantity(arguments, keywords)
            stop = self._optional_number(
                keywords.get("stop", arguments[4] if len(arguments) > 4 else None)
            )
            limit = self._optional_number(
                keywords.get("limit", arguments[3] if len(arguments) > 3 else None)
            )
            order_id = str(
                arguments[0]
                if arguments
                else keywords.get("id", keywords.get("name", f"{side}-{self.current_index}"))
            )
            if stop is None and limit is None:
                self.broker.enter(
                    side,
                    quantity,
                    self.current_candle.close,
                    self.current_index,
                    self.current_candle.timestamp,
                )
            else:
                self._queue_entry(order_id, side, quantity, stop, limit)
            return None
        if name == "close":
            if self._truthy(keywords.get("when", True)):
                position = self.broker.position
                if position is not None:
                    exit_quantity: float | None = self._exit_quantity(
                        arguments, keywords, position.quantity, None, 2
                    )
                    self.broker.partial_close(
                        self.current_candle.close,
                        self.current_index,
                        self.current_candle.timestamp,
                        quantity=exit_quantity,
                        reason="strategy.close",
                    )
            return None
        if name == "close_all":
            if self._truthy(keywords.get("when", True)):
                self.broker.close_all(
                    self.current_candle.close, self.current_index, self.current_candle.timestamp
                )
            return None
        if name == "exit":
            if self._truthy(keywords.get("when", True)) and self.broker.position is not None:
                position = self.broker.position
                stop = self._optional_number(
                    keywords.get("stop", arguments[4] if len(arguments) > 4 else None)
                )
                limit = self._optional_number(
                    keywords.get("limit", arguments[3] if len(arguments) > 3 else None)
                )
                partial_exit_quantity: float | None = self._exit_quantity(
                    arguments, keywords, position.quantity, 2, 3
                )
                if stop is None and limit is None:
                    self.broker.partial_close(
                        self.current_candle.close,
                        self.current_index,
                        self.current_candle.timestamp,
                        quantity=partial_exit_quantity,
                        reason="strategy.exit",
                    )
                else:
                    self.pending_exits[position.side] = _PendingExit(
                        stop, limit, keywords.get("from_entry"), partial_exit_quantity
                    )
            return None
        if name in {"cancel", "cancel_all"}:
            if name == "cancel_all" or not arguments:
                self.pending_entries.clear()
                self.pending_exits.clear()
            else:
                order_id = str(arguments[0])
                self.pending_entries = [
                    order for order in self.pending_entries if order.order_id != order_id
                ]
                self.pending_exits = {
                    key: value
                    for key, value in self.pending_exits.items()
                    if value.from_entry != order_id
                }
            return None
        raise BacktestValidationError(f"unknown strategy member: {name}")

    def _side(self, direction: Any) -> str:
        if isinstance(direction, str) and direction.lower() in {"short", "shortposition"}:
            return "short"
        return "long"

    def _moving_average(self, name: str, series: Sequence[Any], length: int) -> Any:
        numbers = [self._number(value) for value in series[-length:] if value is not None]
        if not numbers:
            return None
        if name == "sma":
            return sum(numbers) / len(numbers)
        if name == "ema":
            result = numbers[0]
            alpha = 2 / (length + 1)
            for number in numbers[1:]:
                result = alpha * number + (1 - alpha) * result
            return result
        if name == "rma":
            result = numbers[0]
            alpha = 1 / length
            for number in numbers[1:]:
                result = alpha * number + (1 - alpha) * result
            return result
        if name == "wma":
            weights = range(1, len(numbers) + 1)
            return sum(
                number * weight for number, weight in zip(numbers, weights, strict=True)
            ) / sum(weights)
        return sum(numbers) / len(numbers)

    def _call_function(
        self,
        function: FunctionDeclaration,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any],
        caller_values: dict[str, Any],
    ) -> Any:
        local = dict(caller_values)
        for index, parameter in enumerate(function.parameters):
            if index < len(arguments):
                value = arguments[index]
            elif parameter.name in keywords:
                value = keywords[parameter.name]
            elif parameter.default is not None:
                value = self._evaluate(parameter.default, caller_values)
            else:
                value = None
            local[parameter.name] = value
        old_values = self.values
        old_active_library = self._active_library
        self.values = local
        self._active_library = self._function_owner.get(id(function))
        try:
            result: Any = None
            for statement in function.body:
                result = self._execute_statement(statement, local)
            return result
        except _Return as returned:
            return returned.value
        finally:
            self.values = old_values
            self._active_library = old_active_library

    def _callee_name(self, expression: Expression) -> str | None:
        if isinstance(expression, (Identifier, NaLiteral)):
            return "na" if isinstance(expression, NaLiteral) else expression.name
        if isinstance(expression, MemberExpression):
            object_name = self._callee_name(expression.object)
            return f"{object_name}.{expression.property}" if object_name else None
        return None

    def _series_key(self, expression: Expression) -> str | None:
        if isinstance(expression, Identifier):
            return expression.name
        if isinstance(expression, MemberExpression):
            parent = self._series_key(expression.object)
            return f"{parent}.{expression.property}" if parent else None
        if isinstance(expression, CallExpression):
            callee = self._callee_name(expression.callee)
            args = ",".join(
                self._series_key(argument) or "?"
                for argument in expression.arguments
                if isinstance(argument, Expression)
            )
            return f"call:{callee}({args})" if callee else None
        if isinstance(expression, HistoryExpression):
            return self._series_key(expression.expression)
        return None

    def _history(self, expression: Expression, offset: int, values: dict[str, Any]) -> Any:
        if offset == 0:
            return self._evaluate(expression, values)
        if offset < 0:
            raise BacktestValidationError("history offset cannot be negative")
        key = self._series_key(expression)
        if key is None:
            return None
        source = self.history.get(key, self.call_history.get(key, []))
        return source[-offset] if len(source) >= offset else None

    def _series_values_for_value(
        self,
        value: Any,
        values: dict[str, Any],
        expression: Expression | None = None,
    ) -> list[Any]:
        key = self._series_key(expression) if expression is not None else None
        if key is None:
            return [value]
        prior = self.history.get(key, self.call_history.get(key, []))
        return [*prior, value]

    def _previous_number(self, expression: Expression, values: dict[str, Any]) -> float | None:
        previous = self._history(expression, 1, values)
        return None if previous is None else self._number(previous)

    def _optional_number(self, value: Any) -> float | None:
        return None if value is None else self._number(value)

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BacktestValidationError(f"expected numeric Pine value, got {value!r}")
        return float(value)

    @staticmethod
    def _truthy(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return bool(value)

    @staticmethod
    def _equal(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        return bool(left == right)


@lru_cache(maxsize=4096)
def _parse_source_cached(source: str) -> Program:
    return Parser(Lexer(source).tokenize()).parse()


def clear_parse_cache() -> None:
    """Clear the process-local parsed-source cache."""

    _parse_source_cached.cache_clear()


def parse_cache_info() -> Any:
    """Return standard cache statistics for diagnostics and benchmarks."""

    return _parse_source_cached.cache_info()


class BacktestEngine:
    """Run a Pine source string against historical or live candles."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        input_overrides: Mapping[str, Any] | None = None,
        library_root: str | Path | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.input_overrides = dict(input_overrides or {})
        self.library_root = Path(library_root).resolve() if library_root is not None else None

    def validate(
        self,
        source: str | Program,
        candles: Iterable[Candle] | None = None,
    ) -> Program:
        """Parse and validate a strategy and optionally its candle stream."""

        program = self.validate_source(source)
        if candles is not None:
            normalized = tuple(candles)
            if not normalized:
                raise BacktestValidationError("at least one candle is required")
            for candle in normalized:
                candle.validate()
        return program

    @staticmethod
    def validate_source(source: str | Program) -> Program:
        if isinstance(source, Program):
            return source
        if not source.strip():
            raise BacktestValidationError("Pine source cannot be empty")
        return _parse_source_cached(source)

    def run(
        self,
        source: str | Program,
        candles: Iterable[Candle],
        *,
        name: str = "pine-strategy",
        symbol: str = "UNKNOWN",
        timeframe: str = "unknown",
        exchange: str = "synthetic",
    ) -> BacktestReport:
        candle_list = tuple(candles)
        program = self.validate(source, candle_list)
        runtime = _PineRuntime(program, self.config, self.input_overrides, self.library_root)
        return runtime.run(
            candle_list,
            name=name,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        )


# A short alias is convenient in notebooks and keeps the public API discoverable.
PineBacktester = BacktestEngine
