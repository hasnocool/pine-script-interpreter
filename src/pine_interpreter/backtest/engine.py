"""A small, deterministic Pine runtime for OHLCV backtests.

The existing project runtime intentionally covers only a basic expression
subset.  This module adds a separate execution path for common Pine
strategies: declarations, series references, moving averages, crossover
signals, and ``strategy.entry``/``strategy.close`` orders. Unsupported
plotting and other nonessential Pine built-ins are accepted only as explicit
runtime approximations so a strategy can be evaluated without implementing
the entire TradingView API.
"""

from __future__ import annotations

import math
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

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
    TypeReference,
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
    "text",
    "chart",
    "timeframe",
    "barstate",
    "box",
    "hline",
    "label",
    "line",
    "matrix",
    "map",
    "position",
    "order",
    "table",
}
# Namespaces that draw or manage locate their handlers inside `_call_named`
# rather than `_member`; every other dotted callee whose first segment is not a
# builtin namespace is treated as a runtime method call on a local value
# (library alias, user-defined type instance, matrix handle, drawing handle, ...).
_BUILTIN_NAMESPACES = _NAMESPACE_NAMES | {
    "vwap",
    "polyline",
    "linefill",
    "draw",
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
_LEGACY_STYLE_NAMES = {
    "area",
    "areabr",
    "circles",
    "columns",
    "cross",
    "dashed",
    "dotted",
    "histogram",
    "linebr",
    "scale",
    "solid",
}

_MISSING = object()
_UNEVALUATED = object()
_OBJECT_TYPE_KEY = "__pine_type__"
_OBJECT_METHODS_KEY = "__pine_methods__"
_DRAWING_HANDLE_KEY = "__pine_drawing_handle__"
_CHART_POINT_KEY = "__pine_chart_point__"


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


@dataclass(frozen=True, slots=True)
class _BrokerState:
    cash: float
    position: _OpenPosition | None
    trades: tuple[Trade, ...]


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
    ) -> bool:
        if quantity <= 0 or not math.isfinite(quantity):
            return False
        if side == "short" and not self.config.allow_short:
            raise BacktestValidationError("short entries are disabled by configuration")
        new_fill = self._fill_price(price, side)
        new_fee = abs(quantity * new_fill) * self.config.fee_rate + self.config.commission_per_trade
        if self.position is not None and self.position.side == side:
            if self.position.entries_count >= self.pyramiding_limit:
                return False
            cash_delta = (
                quantity * new_fill + new_fee if side == "long" else -quantity * new_fill + new_fee
            )
            if self.cash - cash_delta < -1e-9:
                return False
            total_quantity = self.position.quantity + quantity
            self.position = _OpenPosition(
                side,
                total_quantity,
                (self.position.entry_price * self.position.quantity + new_fill * quantity)
                / total_quantity,
                self.position.entry_index,
                self.position.entry_timestamp,
                self.position.entry_fee + new_fee,
                self.position.entries_count + 1,
            )
            self.cash -= cash_delta
            return True

        # Check a reversal as one atomic transaction.  A rejected reverse order
        # must not close the existing position before discovering insufficient cash.
        projected_cash = self.cash
        if self.position is not None:
            close_fill = self._fill_price(price, self.position.side)
            exit_fee = (
                self.position.quantity * abs(close_fill) * self.config.fee_rate
                + self.config.commission_per_trade
            )
            if self.position.side == "long":
                projected_cash += self.position.quantity * close_fill - exit_fee
            else:
                projected_cash -= self.position.quantity * close_fill + exit_fee
        projected_cash += (
            -quantity * new_fill - new_fee if side == "long" else quantity * new_fill - new_fee
        )
        if projected_cash < -1e-9:
            return False
        if self.position is not None:
            self.close(price, index, timestamp, reason="reverse")
        self.cash = projected_cash
        self.position = _OpenPosition(side, quantity, new_fill, index, timestamp, new_fee)
        return True

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

    def snapshot(self) -> _BrokerState:
        """Capture completed accounting state for an interrupted-run artifact."""

        position = self.position
        return _BrokerState(
            cash=self.cash,
            position=(
                _OpenPosition(
                    position.side,
                    position.quantity,
                    position.entry_price,
                    position.entry_index,
                    position.entry_timestamp,
                    position.entry_fee,
                    position.entries_count,
                )
                if position is not None
                else None
            ),
            trades=tuple(self.trades),
        )

    def restore(self, state: _BrokerState) -> None:
        """Restore a state captured at the last completed candle."""

        self.cash = state.cash
        self.position = state.position
        self.trades = list(state.trades)

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


@dataclass(frozen=True, slots=True)
class _ExecutionState:
    broker: _BrokerState
    initial_cash: float
    pending_entries: tuple[_PendingEntry, ...]
    pending_exits: tuple[tuple[str, _PendingExit], ...]
    approximations: tuple[str, ...]


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
        self._callee_name_cache: dict[int, str | None] = {}
        self._series_key_cache: dict[int, str | None] = {}
        self._true_range_cache: tuple[int, list[float]] | None = None
        self.input_cache: dict[str, Any] = {}
        self.input_overrides = dict(input_overrides or {})
        self.approximations: set[str] = set()
        self.library_root = Path(library_root).resolve() if library_root is not None else None
        self.libraries: dict[str, Program] = {}
        self.builtin_library_aliases: set[str] = set()
        self.library_dependencies: set[str] = set()
        self._library_imports: dict[int, dict[str, Program]] = {}
        self._function_owner: dict[int, Program] = {}
        self._library_stack: tuple[Path, ...] = ()
        self._active_library: Program | None = None
        self.functions: dict[str, FunctionDeclaration] = {}
        self.methods: dict[str, FunctionDeclaration] = {}
        self.types: dict[str, TypeDeclaration] = {}
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
                if statement.method:
                    self.methods[statement.name] = statement
                self._function_owner[id(statement)] = self.program
            elif isinstance(statement, TypeDeclaration):
                self.types[statement.name] = statement
                self._register_type_methods(statement, self.program)
            elif isinstance(statement, EnumDeclaration):
                self.enums[statement.name] = self._enum_values(statement)

    def _register_type_methods(self, declaration: TypeDeclaration, owner: Program) -> None:
        for method in declaration.methods:
            self._function_owner[id(method)] = owner

    def _load_import(self, declaration: ImportDeclaration, owner: Program) -> None:
        import_name = declaration.import_path.strip().strip('"').strip("'")
        if self.library_root is None:
            if import_name.startswith(("TradingView/ta/", "TradingView/Strategy/")):
                alias = declaration.alias or Path(import_name).name
                self.builtin_library_aliases.add(alias)
                self.library_dependencies.add(import_name)
                self.approximations.add(f"library.{import_name}.builtin_stub")
                return
            raise BacktestValidationError("Pine library imports require a configured library_root")
        self.library_dependencies.add(import_name)
        if not import_name or Path(import_name).is_absolute() or ".." in Path(import_name).parts:
            raise BacktestValidationError(f"invalid Pine library import path: {import_name!r}")
        library_alias = (
            import_name.split("/")[-2]
            if import_name.split("/")[-1].isdigit()
            else Path(import_name).name
        )
        candidates = [
            self.library_root / f"{import_name}.pine",
            self.library_root / import_name,
        ]
        source_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source_path is None:
            # Archive library names can include a publisher suffix such as
            # ``Name__hash``.  Match the stem exactly before giving up.
            import_parts = Path(import_name).parts
            requested_name = (
                import_parts[-2]
                if len(import_parts) >= 2 and import_parts[-1].isdigit()
                else import_parts[-1]
            )
            requested_stem = requested_name.casefold()
            matches = sorted(
                candidate
                for candidate in self.library_root.rglob("*.pine")
                if candidate.stem.casefold() == requested_stem
            )
            if not matches:
                matches = sorted(
                    candidate
                    for candidate in self.library_root.rglob("*.pine")
                    if candidate.stem.split("__", 1)[0].casefold() == requested_stem
                )
                # Several archive files can share a title prefix.  A candidate
                # that declares `library("<title>")` is the real one, so prefer
                # it over an alphabetically earlier namesake.  The exact case
                # wins first: TradingView's built-in `ta` library is lowercase.
                declared = [
                    candidate
                    for candidate in matches
                    if _declares_library_title(candidate, requested_stem)
                ]
                if not declared:
                    declared = [
                        candidate
                        for candidate in matches
                        if _declares_library_title_any_case(candidate, requested_stem)
                    ]
                if declared:
                    matches = declared
                if matches:
                    self.approximations.add(f"library.{import_name}.local_title_fallback")
            source_path = matches[0] if matches else None
            if source_path is not None and matches and matches[0].stem != requested_stem:
                self.approximations.add(f"library.{import_name}.local_title_fallback")
        if source_path is None:
            if import_name.startswith(("TradingView/ta/", "TradingView/Strategy/")):
                alias = declaration.alias or Path(import_name).name
                self.builtin_library_aliases.add(alias)
                self.approximations.add(f"library.{import_name}.builtin_stub")
                return
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
            alias = declaration.alias or library_alias
            self.libraries[alias] = library_program
            self._library_imports.setdefault(id(owner), {})[alias] = library_program
            for statement in library_program.statements:
                if isinstance(statement, FunctionDeclaration):
                    self._function_owner[id(statement)] = library_program
                    if statement.method:
                        self.methods.setdefault(statement.name, statement)
                elif isinstance(statement, TypeDeclaration):
                    self.types.setdefault(statement.name, statement)
                    self._register_type_methods(statement, library_program)
                elif isinstance(statement, EnumDeclaration):
                    self.enums.setdefault(statement.name, self._enum_values(statement))
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
        self._all_candles = list(candles)
        self.execution_started_at = time.perf_counter()
        completed_state = self._capture_execution_state()
        try:
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
                completed_state = self._capture_execution_state()
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
        except (BacktestExecutionLimitError, BacktestExecutionTimeoutError) as exc:
            self._restore_execution_state(completed_state)
            exc.partial_report = self._build_report(
                name,
                symbol,
                timeframe,
                exchange,
                candles,
                equity_curve,
                partial=True,
            )
            raise
        return self._build_report(
            name,
            symbol,
            timeframe,
            exchange,
            candles,
            equity_curve,
            partial=False,
        )

    def _capture_execution_state(self) -> _ExecutionState:
        return _ExecutionState(
            broker=self.broker.snapshot(),
            initial_cash=self.initial_cash,
            pending_entries=tuple(
                _PendingEntry(
                    order.order_id,
                    order.side,
                    order.quantity,
                    order.stop,
                    order.limit,
                    order.placed_index,
                )
                for order in self.pending_entries
            ),
            pending_exits=tuple(
                (
                    side,
                    _PendingExit(
                        pending.stop,
                        pending.limit,
                        pending.from_entry,
                        pending.quantity,
                    ),
                )
                for side, pending in self.pending_exits.items()
            ),
            approximations=tuple(self.approximations),
        )

    def _restore_execution_state(self, state: _ExecutionState) -> None:
        self.broker.restore(state.broker)
        self.initial_cash = state.initial_cash
        self.pending_entries = list(state.pending_entries)
        self.pending_exits = {
            side: _PendingExit(
                pending.stop,
                pending.limit,
                pending.from_entry,
                pending.quantity,
            )
            for side, pending in state.pending_exits
        }
        self.approximations = set(state.approximations)

    def _build_report(
        self,
        name: str,
        symbol: str,
        timeframe: str,
        exchange: str,
        candles: Sequence[Candle],
        equity_curve: Sequence[EquityPoint],
        *,
        partial: bool,
    ) -> BacktestReport:
        mark_index = len(equity_curve) - 1 if partial and equity_curve else len(candles) - 1
        mark_price = candles[mark_index].close if candles and mark_index >= 0 else self.initial_cash
        final_equity = (
            equity_curve[-1].equity
            if partial and equity_curve
            else self.broker.equity(mark_price)
            if candles
            else self.initial_cash
        )
        position = self.broker.position
        open_position = (
            {
                "side": position.side,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "entry_index": position.entry_index,
                "entry_timestamp": position.entry_timestamp.isoformat(),
                "unrealized_pnl": self.broker.unrealized(mark_price),
            }
            if position is not None
            else None
        )
        return BacktestReport(
            name,
            symbol,
            timeframe,
            exchange,
            self.initial_cash,
            final_equity,
            tuple(self.broker.trades),
            tuple(equity_curve),
            len(equity_curve) if partial else len(candles),
            self.execution_steps,
            tuple(sorted(self.approximations)),
            BACKTEST_RUNTIME_VERSION,
            self.config,
            tuple(sorted(self.library_dependencies)),
            partial,
            open_position,
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
                accepted = self.broker.enter(
                    order.side,
                    order.quantity,
                    fill_price,
                    index,
                    candle.timestamp,
                    reason="entry",
                )
                if not accepted:
                    self.approximations.add("order.rejected_or_ignored")
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
                self._validate_type_value(value, statement.type_annotation, values)
                values[statement.name] = value
                self.persistent[statement.name] = value
                self.persistent_names.add(statement.name)
                return value
            value = self._evaluate(statement.value, values)
            self._validate_type_value(value, statement.type_annotation, values)
            values[statement.name] = value
            self.history.setdefault(statement.name, [])
            return value
        if isinstance(statement, AssignmentStatement):
            if statement.operator in {"=", ":="}:
                value = self._evaluate(statement.value, values)
            else:
                value = self._evaluate(
                    BinaryExpression(
                        statement.location,
                        statement.target,
                        statement.operator[:-1],
                        statement.value,
                    ),
                    values,
                )
            self._assign(statement.target, value, values)
            return value
        if isinstance(statement, TupleDeclaration):
            value = self._evaluate(statement.value, values)
            if not isinstance(value, (list, tuple)):
                self.approximations.add("tuple.assignment_scalar")
                for index, target in enumerate(statement.targets):
                    self._assign(target, value if index == 0 else None, values)
                return value
            if len(value) != len(statement.targets):
                self.approximations.add("tuple.assignment_arity")
            for index, target in enumerate(statement.targets):
                self._assign(target, value[index] if index < len(value) else None, values)
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
            if statement.method:
                self.methods[statement.name] = statement
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
        result: Any = None
        start_value = self._evaluate(start_expression, values)
        if end_expression is None and isinstance(start_value, (list, tuple, str, dict)):
            iterable: Iterable[Any] = (
                start_value.keys() if isinstance(start_value, dict) else start_value
            )
            for item in iterable:
                self._consume_execution_step()
                if isinstance(variable, str):
                    values[variable] = item
                elif isinstance(item, (list, tuple)):
                    for target, element in zip(variable, item, strict=False):
                        self._assign(target, element, values)
                else:
                    self._assign(variable[0], item, values)
                try:
                    result = self._execute_statements(body, values)
                except _Break:
                    break
                except _Continue:
                    pass
            return result
        end_value = self._evaluate(end_expression, values) if end_expression else _MISSING
        step_value = self._evaluate(step_expression, values) if step_expression else 1
        if (
            end_expression is None
            or self._contains_missing(start_value)
            or self._contains_missing(end_value)
            or self._contains_missing(step_value)
        ):
            return None
        start = int(self._number(start_value))
        end = int(self._number(end_value))
        step = int(self._number(step_value))
        if step == 0:
            raise BacktestValidationError("for-loop step cannot be zero")
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
            if expression.name == "dayofweek":
                return self._time_call("weekday", ())
            if expression.name in _NAMESPACE_NAMES:
                return expression.name
            if expression.name in _TYPE_NAMES or expression.name in self.types:
                return expression.name
            if expression.name == "tickerid":
                return self.symbol
            if expression.name == "period":
                return self.timeframe
            if expression.name == "interval":
                match = self.timeframe.lower().rstrip("mhdwS")
                try:
                    return int(match) if match else 1
                except ValueError:
                    return 1
            if expression.name in {"ismonthly", "isweekly", "isdaily", "isintraday"}:
                return {
                    "ismonthly": self.timeframe.endswith("M"),
                    "isweekly": self.timeframe.endswith("w"),
                    "isdaily": self.timeframe.endswith("d"),
                    "isintraday": not self.timeframe.lower().endswith(("d", "w", "m")),
                }[expression.name]
            if expression.name == "time_tradingday":
                self.approximations.add("time.trading_day")
                return (
                    int(
                        self.current_candle.timestamp.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ).timestamp()
                    )
                    if self.current_candle is not None
                    else None
                )
            if expression.name in {"earnings", "dividends"}:
                self.approximations.add(f"nontrading.{expression.name}")
                return 0.0
            if expression.name == "n":
                self.approximations.add("legacy.n_bar_index")
                return self.current_index
            if expression.name == "time_close":
                return (
                    int(self.current_candle.timestamp.timestamp())
                    if self.current_candle is not None
                    else None
                )
            if expression.name == "last_bar_time":
                return (
                    int(self.current_candle.timestamp.timestamp())
                    if self.current_candle is not None
                    else None
                )
            if expression.name == "symbol":
                return self.symbol
            if expression.name == "adjustment":
                return "none"
            if expression.name in {
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            }:
                return {
                    "sunday": 1,
                    "monday": 2,
                    "tuesday": 3,
                    "wednesday": 4,
                    "thursday": 5,
                    "friday": 6,
                    "saturday": 7,
                }[expression.name]
            if expression.name in _LEGACY_STYLE_NAMES:
                return expression.name
            if expression.name in {
                "splits",
                "font",
                "accdist",
                "nvi",
                "ma_up",
                "expValue",
                "_val",
            }:
                self.approximations.add("legacy.identifier_zero")
                return 0.0
            if expression.name == "pvt":
                return self._current_pvt()
            if expression.name == "percentRank":
                self.approximations.add("legacy.percent_rank")
                return 50.0
            if expression.name == "weekofyear":
                return (
                    self.current_candle.timestamp.timetuple().tm_yday if self.current_candle else 0
                )
            if expression.name == "vwap":
                return self._ta_call("vwap", (), (), values, expression)
            if expression.name == "obv":
                return self._current_obv()
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
            if expression.name in {"hl2", "hlc3", "ohlc4", "hlcc4"}:
                high = self._number(values.get("high"))
                low = self._number(values.get("low"))
                close = self._number(values.get("close"))
                open_value = self._number(values.get("open"))
                if expression.name == "hl2":
                    return (high + low) / 2
                if expression.name == "hlc3":
                    return (high + low + close) / 3
                if expression.name == "hlcc4":
                    return (high + low + close + close) / 4
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
            if self._contains_missing(offset_expression):
                return None
            offset = int(self._number(offset_expression))
            if offset < 0:
                self.approximations.add("history.negative_offset_current")
                return self._evaluate(expression.expression, values)
            return self._history(expression.expression, offset, values)
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
        if expression.operator == "not":
            return not self._truthy(value)
        if value is None:
            return None
        if expression.operator == "+":
            return self._number(value)
        if expression.operator == "-":
            return -self._number(value)
        if expression.operator == "not":
            return not self._truthy(value)
        raise BacktestValidationError(f"unsupported unary operator: {expression.operator}")

    def _arithmetic_values(self, left: Any, right: Any, operator: str) -> float | str | None:
        if left is None or right is None:
            return None
        if operator == "+" and isinstance(left, str) and isinstance(right, str):
            return left + right
        left_number, right_number = self._number(left), self._number(right)
        if operator == "+":
            return left_number + right_number
        if operator == "-":
            return left_number - right_number
        if operator == "*":
            return left_number * right_number
        if operator == "/":
            return left_number / right_number if right_number else None
        if operator == "%":
            return left_number % right_number if right_number else None
        if operator == "**":
            try:
                return float(left_number**right_number)
            except (OverflowError, ValueError) as exc:
                raise BacktestValidationError("numeric result is out of range") from exc
        raise BacktestValidationError(f"unsupported binary operator: {operator}")

    def _compare_values(self, left: Any, right: Any, operator: str) -> bool | None:
        if left is None or right is None:
            return None
        if isinstance(left, str) or isinstance(right, str):
            left_value, right_value = str(left), str(right)
            return {
                "<": left_value < right_value,
                "<=": left_value <= right_value,
                ">": left_value > right_value,
                ">=": left_value >= right_value,
            }[operator]
        left_number, right_number = self._number(left), self._number(right)
        return {
            "<": left_number < right_number,
            "<=": left_number <= right_number,
            ">": left_number > right_number,
            ">=": left_number >= right_number,
        }[operator]

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
        if expression.operator in {"<", "<=", ">", ">="} and (
            isinstance(left, (list, tuple)) or isinstance(right, (list, tuple))
        ):
            self.approximations.add("tuple.comparison")
            left_values = left if isinstance(left, (list, tuple)) else [left] * len(right)
            right_values = right if isinstance(right, (list, tuple)) else [right] * len(left)
            return [
                self._compare_values(a, b, expression.operator)
                for a, b in zip(left_values, right_values, strict=False)
            ]
        if expression.operator in {"+", "-", "*", "/", "%", "**"} and (
            isinstance(left, (list, tuple)) or isinstance(right, (list, tuple))
        ):
            self.approximations.add("tuple.arithmetic")
            left_values = left if isinstance(left, (list, tuple)) else [left] * len(right)
            right_values = right if isinstance(right, (list, tuple)) else [right] * len(left)
            return [
                self._arithmetic_values(a, b, expression.operator)
                for a, b in zip(left_values, right_values, strict=False)
            ]
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
        if expression.operator == "+" and (isinstance(left, str) or isinstance(right, str)):
            if left is None or right is None:
                return None
            if isinstance(left, str) and isinstance(right, str):
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
            if name == "ta":
                if expression.property in {"tr", "vwap", "obv"}:
                    return self._ta_call(expression.property, (), (), values, expression)
                if expression.property == "iii":
                    return self._ta_call("iii", (), (), values, expression)
                if expression.property == "pvi":
                    return self._ta_call("pvi", (), (), values, expression)
                if expression.property == "wad":
                    return self._ta_call("wad", (), (), values, expression)
                if expression.property == "wvad":
                    return self._ta_call("wvad", (), (), values, expression)
                if expression.property == "pvt":
                    return self._current_pvt()
                if expression.property == "accdist":
                    return self._current_accdist()
                if expression.property == "nvi":
                    return self._current_nvi()
                if expression.property == "dmi":
                    return [None, None, None]
                return expression.property
            if name == "strategy":
                if expression.property in {"oca", "risk", "commission", "direction"}:
                    return self._strategy_namespace(expression.property)
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
                    return self._timeframe_call("in_seconds", ())
                if expression.property == "change":
                    return self._timeframe_call("change", ())
            if name == "syminfo":
                dynamic = {
                    "tickerid": self.symbol,
                    "ticker": self.symbol,
                    "currency": "USD",
                    "mintick": 0.01,
                    "pointvalue": 1.0,
                    "type": "crypto",
                    "timezone": "Etc/UTC",
                    "country": "US",
                    "root": self.symbol.split(":", 1)[0],
                    "prefix": "",
                    "description": self.symbol,
                    "industry": "",
                    "sector": "",
                    "volatility": 0.0,
                }
                if expression.property == "mincontract":
                    self.approximations.add("syminfo.mincontract")
                    return 0.001
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
                if expression.property in {"isfirstbar", "islastbar"}:
                    self.approximations.add("session.bounds")
                    timestamp = self.current_candle.timestamp if self.current_candle else None
                    return bool(
                        expression.property == "isfirstbar"
                        and timestamp is not None
                        and timestamp.hour == 0
                        and timestamp.minute == 0
                    )
                return expression.property
            if name == "dayofweek":
                return {
                    "sunday": 1,
                    "monday": 2,
                    "tuesday": 3,
                    "wednesday": 4,
                    "thursday": 5,
                    "friday": 6,
                    "saturday": 7,
                }.get(expression.property, expression.property)
            if name == "source":
                return "close"
            if name == "display":
                return 0
            if name == "resolution":
                return "1h"
            if name == "chart":
                if expression.property in {"left_visible_bar_time", "right_visible_bar_time"}:
                    self.approximations.add("chart.visible_bar_time")
                    candles = self._all_candles
                    if not candles:
                        return None
                    timestamp = (
                        candles[0].timestamp
                        if expression.property == "left_visible_bar_time"
                        else candles[-1].timestamp
                    )
                    return int(timestamp.timestamp()) if timestamp is not None else None
            if name in NAMESPACE_MEMBERS:
                value = constant_value(name, expression.property)
                return expression.property if value is None else value
        object_value = self._evaluate(expression.object, values)
        if isinstance(object_value, dict):
            if "__namespace__" in object_value:
                return object_value.get(expression.property, expression.property)
            return object_value.get(expression.property)
        return None

    def _strategy_namespace(self, name: str) -> dict[str, Any]:
        if name == "oca":
            return {"__namespace__": name, "none": "none", "cancel": "cancel", "reduce": "reduce"}
        if name == "commission":
            return {
                "__namespace__": name,
                "percent": "percent",
                "cash_perorder": "cash_perorder",
                "cash_per_contract": "cash_per_contract",
            }
        if name == "direction":
            return {"__namespace__": name, "long": "long", "short": "short"}
        self.approximations.add("strategy.risk")
        return {
            "__namespace__": name,
            "max_drawdown": 0.0,
            "max_drawdown_percent": 0.0,
            "max_intraday_loss": 0.0,
            "max_intraday_loss_percent": 0.0,
            "max_position_size": 0.0,
            "max_position_size_percent": 0.0,
            "max_cons_loss_days": 0,
        }

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
            # TradingView returns `na` when no position is open.  Returning a
            # fabricated 0.0 here turns degenerate stop/limit brackets into
            # invalid crossing orders (0.0 >= 0.0), aborting strategies.
            return position.entry_price if position is not None else None
        if name == "initial_capital":
            return self.initial_cash
        if name == "account_currency":
            self.approximations.add("strategy.account_currency")
            return "USD"
        if name == "eventrades":
            return len(self.broker.trades)
        if name == "default_entry_qty":
            return self.strategy_default_qty_value
        if name == "position_entry_name":
            self.approximations.add("strategy.position_entry_name")
            return ""
        if name == "opentrades":
            return 1 if self.broker.position is not None else 0
        if name == "closedtrades":
            return len(self.broker.trades)
        if name == "equity":
            return self.broker.equity(price)
        if name == "netprofit":
            return self.broker.equity(price) - self.initial_cash
        if name == "netprofit_percent":
            return (self.broker.equity(price) - self.initial_cash) / self.initial_cash * 100
        trades = self.broker.trades
        if name == "avg_trade_percent":
            self.approximations.add("strategy.avg_trade_percent")
            if not trades:
                return 0.0
            return sum(trade.pnl for trade in trades) / len(trades) / self.initial_cash * 100
        if name in {"avg_trade_count", "trade_num"}:
            return len(trades)
        if name == "avgwin_trades":
            wins = [trade for trade in trades if trade.pnl > 0]
            return sum(trade.pnl for trade in wins) / len(wins) if wins else 0.0
        if name == "avgloss_trades":
            losses = [trade for trade in trades if trade.pnl < 0]
            return sum(trade.pnl for trade in losses) / len(losses) if losses else 0.0
        if name == "avgwin_percent":
            wins = [trade for trade in trades if trade.pnl > 0]
            return (
                sum(trade.pnl for trade in wins) / len(wins) / self.initial_cash * 100
                if wins
                else 0.0
            )
        if name == "avgloss_percent":
            losses = [trade for trade in trades if trade.pnl < 0]
            return (
                sum(trade.pnl for trade in losses) / len(losses) / self.initial_cash * 100
                if losses
                else 0.0
            )
        if name == "largestwin":
            return max((trade.pnl for trade in trades), default=0.0)
        if name == "largestloss":
            return min((trade.pnl for trade in trades), default=0.0)
        if name in {
            "max_contracts_held_all",
            "max_contracts_held_long",
            "max_contracts_held_short",
        }:
            # The broker keeps no peak-position history, so report the live
            # position rather than inventing a running maximum.
            self.approximations.add(f"strategy.{name}")
            if position is None:
                return 0.0
            if name == "max_contracts_held_long" and position.side != "long":
                return 0.0
            if name == "max_contracts_held_short" and position.side != "short":
                return 0.0
            return abs(position.quantity)
        if name == "margin_liquidation_price":
            # No margin model, so a margin call can never trigger.
            self.approximations.add("strategy.margin_liquidation_price")
            return 0.0
        if name in {
            "max_cons_loss_days",
            "max_cons_win_days",
            "max_cons_loss_trades",
            "max_cons_win_trades",
            "max_cons_loss",
            "max_cons_profit",
            "max_cons_loss_percent",
            "max_cons_profit_percent",
        }:
            self.approximations.add(f"strategy.{name}")
            return 0.0
        if name in {"wintrades_percent", "losstrades_percent"}:
            if not trades:
                return 0.0
            win_count = sum(trade.pnl > 0 for trade in trades)
            total_count = len(trades)
            if name == "wintrades_percent":
                return win_count / total_count * 100
            return (total_count - win_count) / total_count * 100
        if name == "openprofit":
            return self.broker.unrealized(price)
        if name == "openprofit_percent":
            return self.broker.unrealized(price) / self.initial_cash * 100
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
        receiver_known = False
        if isinstance(expression.callee, MemberExpression):
            receiver_known = True
            try:
                receiver = self._evaluate(expression.callee.object, values)
            except BacktestValidationError:
                receiver = None
                receiver_known = False
            if (
                isinstance(receiver, list)
                and expression.callee.property
                in {
                    "remove",
                    "insert",
                    "get",
                    "set",
                    "push",
                    "pop",
                    "shift",
                    "unshift",
                    "clear",
                    "sort",
                    "copy",
                    "sum",
                    "min",
                    "max",
                    "avg",
                    "size",
                    "first",
                    "last",
                }
                and not (
                    # A matrix is a list of rows; `get`/`set` take (row, column)
                    # and must not be handled as the 1-D array fast path.
                    expression.callee.property in {"get", "set"}
                    and receiver
                    and isinstance(receiver[0], list)
                )
            ):
                return self._array_call(
                    expression.callee.property, [receiver, *arguments], keywords
                )
        callee = self._callee_name(expression.callee)
        # Reuse the receiver already evaluated above: re-evaluating it would
        # run side effects twice, e.g. `array.shift().delete()` draining two
        # elements per iteration.
        resolved_receiver = receiver if receiver_known else _UNEVALUATED
        if callee is not None and "." in callee:
            namespace = callee.split(".", 1)[0]
            if namespace not in _BUILTIN_NAMESPACES:
                dynamic_callee = self._dynamic_callee(expression.callee, values, resolved_receiver)
                if dynamic_callee is not None:
                    target, method_name = dynamic_callee
                    result = self._object_method(
                        target,
                        method_name,
                        arguments,
                        keywords,
                        values,
                    )
                    if result is not _MISSING:
                        self._record_call_series(expression, result)
                        return result
                    # Method not implemented on the runtime value; fall through
                    # to `_call_named`, which resolves user-defined type
                    # constructors (State.new) and surfaces an explicit
                    # "unknown Pine builtin" error for anything else.
        if callee is None:
            dynamic_callee = self._dynamic_callee(expression.callee, values, resolved_receiver)
            if dynamic_callee is not None:
                target, method_name = dynamic_callee
                result = self._object_method(
                    target,
                    method_name,
                    arguments,
                    keywords,
                    values,
                )
                if result is _MISSING:
                    raise BacktestValidationError(f"unsupported Pine call target: {method_name}")
                self._record_call_series(expression, result)
                return result
            raise BacktestValidationError("unsupported Pine call target")
        result = self._call_named(callee, arguments, argument_nodes, keywords, values, expression)
        self._record_call_series(expression, result)
        return result

    def _record_call_series(self, expression: Expression, result: Any) -> None:
        """Remember a call's result so `history` offsets resolve for it.

        Every dispatch path must record: a `ta.*` call routed through
        `_call_named` and a method call answered by `_object_method` are the
        same kind of series node to later `[1]` lookups.
        """

        key = self._series_key(expression)
        if key is not None and key.startswith("call:") and key not in self.call_seen:
            self.call_history.setdefault(key, []).append(result)
            self.call_seen.add(key)

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

    def _lookup_type(self, name: str) -> TypeDeclaration | None:
        """Resolve a local, imported, or namespaced Pine user-defined type."""

        if self._active_library is not None and "." not in name:
            imported = next(
                (
                    statement
                    for statement in self._active_library.statements
                    if isinstance(statement, TypeDeclaration) and statement.name == name
                ),
                None,
            )
            if imported is not None:
                return imported
        direct = self.types.get(name)
        if direct is not None:
            return direct
        if "." in name:
            namespace, type_name = name.split(".", 1)
            library = self._library_imports.get(id(self._active_library), {}).get(namespace)
            if library is None:
                library = self.libraries.get(namespace)
            if library is not None:
                return next(
                    (
                        statement
                        for statement in library.statements
                        if isinstance(statement, TypeDeclaration) and statement.name == type_name
                    ),
                    None,
                )
        return None

    def _validate_type_value(
        self,
        value: Any,
        annotation: TypeReference | None,
        values: dict[str, Any],
    ) -> Any:
        """Apply conservative runtime checks for user-defined object types."""

        if annotation is None or value is None:
            return value
        declaration = self._lookup_type(annotation.name)
        if declaration is None:
            return value
        if not isinstance(value, dict) or value.get(_OBJECT_TYPE_KEY) != declaration.name:
            raise BacktestValidationError(
                f"expected {annotation.name} object, got {type(value).__name__}"
            )
        return value

    def _construct_type(
        self,
        declaration: TypeDeclaration,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any],
        values: dict[str, Any],
        node: Node,
    ) -> dict[str, Any]:
        """Construct a small dictionary-backed approximation of a Pine object."""

        field_names = {field.name for field in declaration.fields}
        unknown = set(keywords) - field_names
        if unknown:
            raise BacktestValidationError(
                f"unknown field for {declaration.name}: {sorted(unknown)[0]}"
            )
        if len(arguments) > len(declaration.fields):
            raise BacktestValidationError(f"too many arguments for {declaration.name}.new")
        object_value: dict[str, Any] = {
            _OBJECT_TYPE_KEY: declaration.name,
            _OBJECT_METHODS_KEY: {method.name: method for method in declaration.methods},
        }
        for index, field in enumerate(declaration.fields):
            if field.name in keywords:
                value = keywords[field.name]
            elif index < len(arguments):
                value = arguments[index]
            elif field.value is not None:
                value = self._evaluate(field.value, values)
            else:
                value = None
            self._validate_type_value(value, field.type_annotation, values)
            object_value[field.name] = value
        return object_value

    def _chart_point_call(self, name: str, arguments: Sequence[Any]) -> Any:
        member = name[len("chart.point.") :] if name.startswith("chart.point.") else name
        if member in {"from_index", "from_time"} and len(arguments) >= 2:
            self.approximations.add("chart.point")
            return {
                _CHART_POINT_KEY: True,
                "index": arguments[0],
                "price": arguments[1],
            }
        self.approximations.add(f"chart.point.{member}")
        return None

    def _call_named(
        self,
        name: str,
        arguments: list[Any],
        argument_nodes: Sequence[Expression],
        keywords: dict[str, Any],
        values: dict[str, Any],
        node: Node,
    ) -> Any:
        if name.endswith(".new"):
            type_name = name[:-4]
            declaration = self._lookup_type(type_name)
            if declaration is not None:
                return self._construct_type(declaration, arguments, keywords, values, node)
        if name == "strategy":
            initial_cash = keywords.get("initial_capital")
            if initial_cash is not None:
                self.initial_cash = self._number(initial_cash)
                if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
                    self.approximations.add("strategy.initial_capital_fallback")
                    self.initial_cash = self.config.initial_cash
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
            "study",
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
            value = (
                arguments[0]
                if arguments
                else keywords.get("defval", keywords.get("default", keywords.get("value")))
            )
            if value is None:
                return None
            if name == "input":
                type_name = str(keywords.get("type", ""))
                if type_name in {"input.integer", "integer"}:
                    return int(self._number(value))
                if type_name in {"input.float", "float"}:
                    return self._number(value)
                if type_name in {"input.bool", "bool"}:
                    return self._truthy(value)
                if type_name in {"input.string", "string"}:
                    return str(value)
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
        # User-defined functions take precedence over legacy builtin aliases.
        # This matters for helpers such as ``donchian(len)`` in v2/v3 scripts.
        user_function = self._lookup_function(name)
        if user_function is not None:
            return self._call_function(user_function, arguments, keywords, values)
        if name in {
            "dev",
            "bb",
            "mom",
            "obv",
            "correlation",
            "percentrank",
            "percentRank",
            "variance",
            "anchored_vwap",
            "alma",
            "swma",
        }:
            alias = {"dev": "stdev", "bb": "bb", "mom": "momentum"}.get(name, name)
            return self._ta_call(alias, arguments, argument_nodes, values, node)
        if name == "stoch":
            return self._ta_call("stoch_scalar", arguments, argument_nodes, values, node)
        if name in {"linreg", "ta.linreg"}:
            return self._ta_call("linreg_scalar", arguments, argument_nodes, values, node)
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
            "bbw",
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
            "tsi",
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
            "dema",
            "tema",
            "trima",
            "frama",
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
        if name.startswith("timeframe."):
            return self._timeframe_call(name[10:], arguments)
        if name in {
            "plot",
            "plotarrow",
            "plotbar",
            "plotcandle",
            "plotchar",
            "plotkline",
            "plotshape",
            "hline",
            "bgcolor",
            "barcolor",
            "fill",
        } or name.startswith("log."):
            self.approximations.add("plotting.non_trading")
            return None
        if name == "tostring":
            return self._string_call("tostring", arguments)
        if name == "dayofweek":
            return self._time_call("weekday", arguments)
        if name.endswith(
            (
                ".tradeCount",
                ".longWinPercent",
                ".shortWinPercent",
                ".breakEvenCount",
                ".breakEvenPercent",
                ".maxDrawdownRealized",
                ".totalPipReturn",
                ".truncate",
                ".toWhole",
                ".fillCell",
            )
        ) and name.rsplit(".", 1)[0] in {"l_zen", "zen"}:
            self.approximations.add("library.zen.trade_statistics_compat")
            member = name.rsplit(".", 1)[1]
            trades = self.broker.trades
            if member == "tradeCount":
                return len(trades)
            if member == "fillCell":
                return None
            long_trades = [trade for trade in trades if trade.side == "long"]
            short_trades = [trade for trade in trades if trade.side == "short"]
            if member == "longWinPercent":
                return (
                    100 * sum(trade.pnl > 0 for trade in long_trades) / len(long_trades)
                    if long_trades
                    else 0.0
                )
            if member == "shortWinPercent":
                return (
                    100 * sum(trade.pnl > 0 for trade in short_trades) / len(short_trades)
                    if short_trades
                    else 0.0
                )
            if member in {"breakEvenCount", "breakEvenPercent"}:
                if not arguments or self._contains_missing(arguments[0]):
                    return 0 if member == "breakEvenCount" else 0.0
                allowance = abs(self._number(arguments[0]))
                count = sum(
                    abs(trade.exit_price - trade.entry_price) <= allowance * 0.01
                    for trade in trades
                )
                return (
                    count
                    if member == "breakEvenCount"
                    else (100 * count / len(trades) if trades else 0.0)
                )
            if member == "maxDrawdownRealized":
                equity = self.initial_cash
                peak = equity
                drawdown = 0.0
                for trade in trades:
                    equity += trade.pnl
                    peak = max(peak, equity)
                    drawdown = max(drawdown, 100 * (peak - equity) / peak)
                return drawdown
            if member == "totalPipReturn":
                return (
                    sum(
                        (trade.exit_price - trade.entry_price) * (1 if trade.side == "long" else -1)
                        for trade in trades
                    )
                    * 10
                )
            if member == "truncate":
                if not arguments or self._contains_missing(arguments[0]):
                    return None
                decimals = (
                    int(self._number(arguments[1]))
                    if len(arguments) > 1 and not self._contains_missing(arguments[1])
                    else 2
                )
                factor = 10**decimals
                return int(self._number(arguments[0]) * factor) / factor
            if member == "toWhole":
                if not arguments or self._contains_missing(arguments[0]):
                    return None
                return self._number(arguments[0]) / 0.1
        if name.endswith(".averageRR") and name.rsplit(".", 1)[0] in {"l_zen", "zen"}:
            winners = [trade.pnl for trade in self.broker.trades if trade.pnl > 0]
            losers = [trade.pnl for trade in self.broker.trades if trade.pnl < 0]
            if not winners or not losers:
                return None
            self.approximations.add("library.zen.average_rr_compat")
            return (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))
        if name.endswith(".getPositionSize") and name.rsplit(".", 1)[0] in {"l_zen", "zen"}:
            if (
                len(arguments) < 2
                or self.current_candle is None
                or self._contains_missing(arguments[0])
                or self._contains_missing(arguments[1])
            ):
                return 0.0
            risk = self._number(arguments[0])
            stop_delta = abs(self._number(arguments[1]))
            balance = (
                self._number(arguments[2])
                if len(arguments) > 2 and arguments[2] is not None
                else self.broker.equity(self.current_candle.close)
            )
            step = (
                self._number(arguments[6])
                if len(arguments) > 6 and arguments[6] is not None
                else 0.001
            )
            if stop_delta <= 0 or step <= 0 or balance <= 0:
                return 0.0
            self.approximations.add("library.zen.position_size_same_currency")
            return math.floor((balance * risk / 100.0) / stop_delta / step) * step
        if name.endswith(".getPullbackBarCount") and name.rsplit(".", 1)[0] in {"l_zen", "zen"}:
            if len(arguments) < 2:
                return None
            lookback = max(0, int(self._number(arguments[0])))
            direction = int(self._number(arguments[1]))
            count = 0
            closes = self.history.get("close", [])
            opens = self.history.get("open", [])
            for offset in range(1, lookback + 1):
                if len(closes) < offset or len(opens) < offset:
                    continue
                close = closes[-offset]
                open_value = opens[-offset]
                if close is None or open_value is None:
                    continue
                if (direction > 0 and close > open_value) or (direction < 0 and close < open_value):
                    count += 1
            self.approximations.add("library.zen.pullback_compat")
            return count
        if name == "pvt":
            return self._current_pvt()
        if name in {"heikenashi", "heikinashi", "ha"}:
            self.approximations.add("heikenashi.ohlc")
            return self._heikinashi_values()
        if name in {"rising", "falling"} and len(arguments) >= 2:
            return self._rising_falling(name, arguments, argument_nodes, values)
        if name in {"color", "label", "line", "box", "table"}:
            if name == "color":
                return arguments[0] if arguments else None
            return self._new_drawing_handle(name, arguments, keywords)
        if name in {"max_bars_back", "renko", "tickerid"}:
            if name == "tickerid":
                return self.symbol
            self.approximations.add(f"nontrading.{name}")
            return self._last_number(self._ohlcv_series("close"))
        if name.startswith("chart.point"):
            return self._chart_point_call(name, arguments)
        if name.startswith(
            ("line.", "label.", "box.", "table.", "ticker.", "draw.", "linefill.", "polyline.")
        ):
            drawing_namespace, drawing_member = name.split(".", 1)
            if drawing_namespace == "ticker" and drawing_member in {"heikinashi", "renko"}:
                self.approximations.add(f"ticker.{drawing_member}")
                return self._heikinashi_values()
            if drawing_member == "new":
                return self._new_drawing_handle(drawing_namespace, arguments, keywords)
            if drawing_member == "copy" and drawing_namespace in {
                "line",
                "label",
                "box",
                "polyline",
            }:
                source = arguments[0] if arguments else None
                if isinstance(source, dict) and _DRAWING_HANDLE_KEY in source:
                    # A copy is an independent drawing with the same geometry.
                    return dict(source)
                return None
            if drawing_member.startswith(("style_", "location_")):
                return drawing_member
            return None
        if name == "na":
            return not arguments or self._contains_missing(arguments[0])
        if name == "nz":
            return (
                arguments[0]
                if arguments and arguments[0] is not None
                else (arguments[1] if len(arguments) > 1 else 0)
            )
        if name == "runtime.error":
            raise BacktestValidationError(
                str(arguments[0]) if arguments else "Pine runtime.error was called"
            )
        if name == "fixnan":
            return (
                arguments[0]
                if arguments and arguments[0] is not None
                else (arguments[1] if len(arguments) > 1 else 0)
            )
        if name in {
            "abs",
            "sign",
            "log",
            "log10",
            "exp",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "todegrees",
            "toradians",
            "round_to_mintick",
        }:
            return self._math_call(name, arguments)
        if name.startswith("math."):
            return self._math_call(name[5:], arguments)
        if name.startswith("ta."):
            return self._ta_call(name[3:], arguments, argument_nodes, values, node)
        if "." in name:
            namespace, member = name.split(".", 1)
            if namespace in self.builtin_library_aliases:
                self.approximations.add(f"library.{namespace}.{member}")
                if member in {
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
                    "bb",
                    "bbw",
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
                    "linreg",
                    "stdev",
                    "dema",
                    "tema",
                    "trima",
                    "frama",
                }:
                    return self._ta_call(member, arguments, argument_nodes, values, node)
                if member == "priceToTicks" and len(arguments) >= 2:
                    tick = self._number(arguments[1])
                    return abs(self._number(arguments[0])) / tick if tick else 0.0
                if member == "calcPositionSizeByStopLossTicks":
                    return self._entry_quantity([], {})
                return None
        if name.startswith(
            ("fpData.", "fp.", "pocRow.", "vahRow.", "valRow.", "deltaRow.", "volRow.")
        ):
            self.approximations.add("footprint.zero")
            return 0.0
        if name == "Strategy.closeAllAtEndOfSession":
            self.approximations.add("nontrading.close_at_session_end")
            return None
        if name.startswith("strategy.closedtrades."):
            suffix = name[len("strategy.closedtrades.") :]
            if arguments and self._contains_missing(arguments[0]):
                return None
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
        if name.startswith("strategy.risk."):
            risk_member = name.rsplit(".", 1)[-1]
            self.approximations.add(f"strategy.risk.{risk_member}")
            if risk_member == "allow_entry_in":
                return True
            if risk_member == "max_intraday_filled_orders":
                return self.config.pyramiding
            return 0.0
        if name == "strategy.default_entry_qty":
            return self._entry_quantity([], {})
        if name == "strategy.convert_to_symbol":
            self.approximations.add("strategy.convert_to_symbol")
            cash = self._number(arguments[0]) if arguments else self.initial_cash
            close = self._last_number(self._ohlcv_series("close"))
            return cash / close if close else 0.0
        if name == "strategy.convert_to_account":
            # The synthetic exchange quotes the account and the symbol in the
            # same currency, so the conversion is the identity.
            self.approximations.add("strategy.convert_to_account")
            return self._number(arguments[0]) if arguments else 0.0
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
                self.input_cache[key] = (
                    arguments[0]
                    if arguments
                    else keywords.get("defval", keywords.get("default", keywords.get("value")))
                )
            return self.input_cache[key]
        if name.startswith("array."):
            return self._array_call(name[6:], arguments, keywords)
        if name.startswith("matrix."):
            return self._matrix_method_call(name[7:], arguments, keywords)
        if name.startswith("map."):
            return self._map_call(name[4:], arguments)
        if name.startswith("str."):
            return self._string_call(name[4:], arguments)
        if name.startswith("color."):
            member = name[6:]
            if member in {"r", "g", "b", "a"}:
                return self._color_component(arguments[0] if arguments else None, member)
            if member == "rgb" or member == "rgba":
                if any(self._contains_missing(value) for value in arguments):
                    return None
                return tuple(self._number(value) for value in arguments)
            if member == "new":
                return arguments[0] if arguments else None
            return member
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
            "request.economic",
            "request.splits",
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
            "weekofyear",
        }:
            return self._time_call(name, arguments)
        if name == "hlcc4":
            candle = self.current_candle
            if candle is None:
                return None
            return (candle.high + candle.low + candle.close + candle.close) / 4
        if name == "random":
            self.approximations.add("random.deterministic_midpoint")
            if len(arguments) >= 2:
                low = self._number(arguments[0])
                high = self._number(arguments[1])
                return (low + high) / 2
            return 0.5
        if name == "offset" and arguments:
            self.approximations.add("offset.approximation")
            offset = int(self._number(arguments[1])) if len(arguments) > 1 else 0
            if offset > 0 and argument_nodes:
                previous = self._history(argument_nodes[0], offset, values)
                if previous is not None:
                    return previous
            return arguments[0]
        if name in {"security", "request.security", "request.security_lower_tf"}:
            self.approximations.add("request.security")
            expression = keywords.get("expression")
            if expression is None and len(arguments) >= 3:
                expression = arguments[2]
            if expression is None and arguments:
                expression = arguments[-1]
            return expression if expression is not None else 0.0
        if name in {
            "earnings",
            "dividends",
            "splits",
            "weekofyear",
            "pointfigure",
            "linebreak",
            "prevHigh",
            "inDateRange",
            "request.footprint",
        }:
            self.approximations.add(f"nontrading.{name}")
            return 0.0
        if name == "percentile_nearest_rank":
            self.approximations.add("percentile.nearest_rank_approximation")
            return 50.0
        if name == "tsi":
            self.approximations.add("ta.tsi")
            return 0.0
        if name == "font":
            self.approximations.add("nontrading.font")
            return arguments[0] if arguments else None
        if name == "tonumber":
            if not arguments or arguments[0] is None:
                return None
            value = arguments[0]
            if isinstance(value, (int, float)):
                return self._number(value)
            text = str(value).strip()
            try:
                return float(text)
            except ValueError:
                number = ""
                for character in text:
                    if character.isdigit() or character == ".":
                        number += character
                    else:
                        break
                if not number:
                    return None
                self.approximations.add("tonumber.timeframe")
                return float(number)
        if name == "abs":
            return self._math_call("abs", arguments)
        if name in {"max", "min", "round", "floor", "ceil", "sqrt", "pow", "avg"}:
            return self._math_call(name, arguments)
        if "." in name:
            path = name.split(".")
            target_name, *property_path = path
            target = values.get(target_name, _MISSING)
            for property_name in property_path[:-1]:
                if not isinstance(target, dict):
                    target = _MISSING
                    break
                target = target.get(property_name, _MISSING)
            if target is not _MISSING:
                dynamic = self._object_method(
                    target,
                    property_path[-1],
                    arguments,
                    keywords,
                    values,
                )
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

    def _color_component(self, value: Any, component: str) -> float:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            index = {"r": 0, "g": 1, "b": 2, "a": 3}.get(component, 0)
            return self._number(value[index]) if index < len(value) else 0.0
        text = str(value or "")
        if text.startswith("#") and len(text) >= 7:
            try:
                channels = [int(text[index : index + 2], 16) for index in (1, 3, 5)]
                if component == "a":
                    return float(int(text[7:9], 16) / 255) if len(text) >= 9 else 255.0
                return float(channels[{"r": 0, "g": 1, "b": 2}.get(component, 0)])
            except ValueError:
                return 0.0
        named = {
            "black": (0, 0, 0),
            "white": (255, 255, 255),
            "red": (255, 0, 0),
            "green": (0, 128, 0),
            "blue": (0, 0, 255),
            "yellow": (255, 255, 0),
            "orange": (255, 165, 0),
            "silver": (192, 192, 192),
            "navy": (0, 0, 128),
            "lime": (0, 255, 0),
        }
        if text.casefold() in named:
            return float(named[text.casefold()][{"r": 0, "g": 1, "b": 2}.get(component, 0)])
        return 0.0

    def _math_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if name != "sum" and any(isinstance(value, (list, tuple)) for value in arguments):
            self.approximations.add("math.tuple_input_na")
            return None
        if name == "sum" and arguments and isinstance(arguments[0], (list, tuple)):
            return sum(self._number(value) for value in arguments[0] if value is not None)
        if arguments and isinstance(arguments[0], (list, tuple)) and name != "sum":
            if not arguments[0]:
                return None
            return [self._math_call(name, [element, *arguments[1:]]) for element in arguments[0]]
        if not arguments or (arguments[0] is None and name not in {"sum"}):
            return None
        numbers = [self._number(value) for value in arguments if value is not None]
        if name == "max":
            return max(numbers) if numbers else None
        if name == "min":
            return min(numbers) if numbers else None
        if name == "avg":
            return sum(numbers) / len(numbers) if numbers else None
        if name == "sum":
            return sum(numbers)
        if not numbers:
            return None
        value = numbers[0]
        if name == "random":
            self.approximations.add("random.deterministic_midpoint")
            return (numbers[0] + numbers[1]) / 2 if len(numbers) > 1 else 0.5
        if name == "abs":
            return abs(value)
        if name == "sign":
            return 0 if value == 0 else 1 if value > 0 else -1
        if name == "log":
            return math.log(value) if value > 0 else None
        if name == "log10":
            return math.log10(value) if value > 0 else None
        if name == "exp":
            try:
                return math.exp(value)
            except OverflowError:
                self.approximations.add("math.exp.saturated")
                return sys.float_info.max
        if name == "sqrt":
            return math.sqrt(value) if value >= 0 else None
        if name == "sin":
            return math.sin(value)
        if name == "cos":
            return math.cos(value)
        if name == "tan":
            return math.tan(value)
        if name == "asin":
            return math.asin(value) if -1 <= value <= 1 else None
        if name == "acos":
            return math.acos(value) if -1 <= value <= 1 else None
        if name == "atan":
            return math.atan(value)
        if name == "todegrees":
            return math.degrees(value)
        if name == "toradians":
            return math.radians(value)
        if name == "round":
            digits = int(self._number(arguments[1])) if len(arguments) > 1 else 0
            return round(value, digits)
        if name == "round_to_mintick":
            return round(value)
        if name == "floor":
            return math.floor(value)
        if name == "ceil":
            return math.ceil(value)
        if name == "pow" and len(numbers) > 1:
            try:
                return numbers[0] ** numbers[1]
            except OverflowError:
                self.approximations.add("math.pow.saturated_na")
                return None
            except ValueError:
                return None
        return value

    def _extract_point(self, value: Any) -> tuple[Any, Any] | None:
        if not isinstance(value, dict):
            return None
        if value.get(_CHART_POINT_KEY):
            return (value.get("index"), value.get("price"))
        if value.get(_DRAWING_HANDLE_KEY):
            index = value.get("index") or value.get("x") or value.get("left")
            price = value.get("price") or value.get("y") or value.get("top")
            return (index, price)
        return None

    def _new_drawing_handle(
        self,
        kind: str,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any],
    ) -> dict[str, Any]:
        field_names = {
            "line": ("x1", "y1", "x2", "y2", "extend", "color", "style", "width"),
            "label": ("x", "y", "text", "tooltip", "textcolor", "style", "size", "color"),
            "box": ("left", "top", "right", "bottom", "border_color", "border_width", "bgcolor"),
            "table": ("columns", "rows", "bgcolor", "frame_color", "frame_width"),
            "linefill": ("line1", "line2", "color"),
            "ticker": ("symbol", "text"),
            "draw": ("value",),
            "polyline": ("points", "line_color", "line_width"),
        }.get(kind, ())
        # Pine v6 drawing constructors accept chart.point instances for the
        # positional coordinates.  A point supplies both members of an X/Y
        # pair, so it advances the pair rather than the single slot.
        position_fields = {
            "line": ("x1", "y1", "x2", "y2"),
            "label": ("x", "y"),
            "box": ("left", "top", "right", "bottom"),
        }.get(kind)
        handle: dict[str, Any] = {_DRAWING_HANDLE_KEY: kind}
        if position_fields is not None:
            pair_cursor = 0
            for index, value in enumerate(arguments[: len(position_fields)]):
                point = self._extract_point(value)
                if point is not None:
                    pair_start = pair_cursor * 2
                    handle[position_fields[pair_start]] = point[0]
                    handle[position_fields[pair_start + 1]] = point[1]
                    pair_cursor += 1
                else:
                    handle[position_fields[index]] = value
            for index, value in enumerate(
                arguments[len(position_fields) :], start=len(position_fields)
            ):
                if index < len(field_names):
                    handle[field_names[index]] = value
        else:
            for index, value in enumerate(arguments):
                if index < len(field_names):
                    handle[field_names[index]] = value
        handle.update(keywords)
        # Normalise any chart.point values supplied as keyword arguments.
        horizontal = {"x", "x1", "x2", "left", "right"}
        for key in ("x", "y", "x1", "y1", "x2", "y2", "left", "top", "right", "bottom"):
            if key in handle:
                point = self._extract_point(handle[key])
                if point is not None:
                    handle[key] = point[0] if key in horizontal else point[1]
        if kind == "polyline" and "points" not in handle:
            points: list[tuple[Any, Any]] = []
            for value in arguments:
                if value is None:
                    continue
                point = self._extract_point(value)
                if point is not None:
                    points.append(point)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        item_point = self._extract_point(item)
                        if item_point is not None:
                            points.append(item_point)
            handle["points"] = points
        self.approximations.add("drawing.handles")
        return handle

    def _matrix_method_call(
        self,
        name: str,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any] | None = None,
    ) -> Any:
        args = list(arguments)
        named = dict(keywords or {})
        if "id" in named and (not args or not isinstance(args[0], list)):
            args.insert(0, named.pop("id"))
        if name in {"new", "new_float", "new_int"}:
            self.approximations.add("matrix.approximation")
            if not args:
                if "rows" not in named and "columns" not in named:
                    return []
                rows = max(0, int(self._number(named.get("rows", 0))))
                columns = max(0, int(self._number(named.get("columns", 0))))
                value = named.get("initial_value", 0.0)
                return [[value for _ in range(columns)] for _ in range(rows)]
            rows = max(0, int(self._number(args[0]))) if len(args) >= 1 else 0
            columns = max(0, int(self._number(args[1]))) if len(args) >= 2 else 0
            value = args[2] if len(args) >= 3 else 0.0
            return [[value for _ in range(columns)] for _ in range(rows)]
        if not args or not isinstance(args[0], list):
            self.approximations.add("matrix.approximation")
            return None
        matrix = args[0]
        self.approximations.add("matrix.method_call")
        if name == "rows":
            return len(matrix)
        if name == "columns":
            return len(matrix[0]) if matrix and isinstance(matrix[0], list) else 0
        if name == "row" and len(args) >= 2 and args[1] is not None:
            row = int(self._number(args[1]))
            return matrix[row] if 0 <= row < len(matrix) else None
        if name == "col" and len(args) >= 2 and args[1] is not None:
            column = int(self._number(args[1]))
            if not matrix or not isinstance(matrix[0], list):
                return None
            width = max(len(row) for row in matrix)
            if not 0 <= column < width:
                return None
            return [row[column] for row in matrix if column < len(row)]
        if name in {"add_row", "append"}:
            row_array = args[1] if len(args) > 1 else named.get("array_id")
            if row_array is not None:
                if isinstance(row_array, (list, tuple)):
                    matrix.append(list(row_array))
                else:
                    matrix.append([row_array])
                return matrix
            return matrix
        if name == "remove_row" and len(args) >= 2 and args[1] is not None:
            row = int(self._number(args[1]))
            if 0 <= row < len(matrix):
                matrix.pop(row)
            return matrix
        if name == "add_col":
            column_array = args[1] if len(args) > 1 else named.get("array_id")
            items = (
                list(column_array)
                if isinstance(column_array, (list, tuple))
                else ([column_array] if column_array is not None else [])
            )
            for row_index in range(len(matrix)):
                if not isinstance(matrix[row_index], list):
                    continue
                if row_index < len(items):
                    matrix[row_index].append(items[row_index])
                else:
                    matrix[row_index].append(None)
            return matrix
        if name == "remove_col" and len(args) >= 2 and args[1] is not None:
            column = int(self._number(args[1]))
            for row in matrix:
                if isinstance(row, list) and 0 <= column < len(row):
                    row.pop(column)
            return matrix
        if name == "get" and len(args) >= 3:
            row = int(self._number(args[1]))
            column = int(self._number(args[2]))
            return (
                matrix[row][column]
                if 0 <= row < len(matrix)
                and isinstance(matrix[row], list)
                and 0 <= column < len(matrix[row])
                else None
            )
        if name == "set" and len(args) >= 4:
            row = int(self._number(args[1]))
            column = int(self._number(args[2]))
            if (
                0 <= row < len(matrix)
                and isinstance(matrix[row], list)
                and 0 <= column < len(matrix[row])
            ):
                matrix[row][column] = args[3]
            return matrix
        self.approximations.add(f"matrix.{name}")
        return None

    def _object_method(
        self,
        target: Any,
        name: str,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any] | None = None,
        values: dict[str, Any] | None = None,
    ) -> Any:
        if isinstance(target, dict) and target.get(_OBJECT_TYPE_KEY):
            methods = target.get(_OBJECT_METHODS_KEY, {})
            method = methods.get(name) if isinstance(methods, dict) else None
            if method is not None:
                return self._call_function(
                    method,
                    [target, *arguments],
                    keywords or {},
                    values if values is not None else self.values,
                )
        if isinstance(target, dict) and _DRAWING_HANDLE_KEY in target:
            if name.startswith("get_"):
                return target.get(name[4:])
            if name.startswith("set_"):
                field_name = name[4:]
                if field_name in {"xy1", "xy2", "lefttop", "rightbottom"} and arguments:
                    first, second = {
                        "xy1": ("x1", "y1"),
                        "xy2": ("x2", "y2"),
                        "lefttop": ("left", "top"),
                        "rightbottom": ("right", "bottom"),
                    }[field_name]
                    point = self._extract_point(arguments[0])
                    if point is not None:
                        target[first] = point[0]
                        target[second] = point[1]
                    elif len(arguments) >= 2:
                        target[first] = arguments[0]
                        target[second] = arguments[1]
                    else:
                        target[field_name] = arguments[0]
                elif arguments:
                    target[field_name] = arguments[0]
                return target
            if name in {
                "delete",
                "cell",
                "clear",
                "merge_cells",
                "set_bgcolor",
                "set_border_color",
            }:
                return None
        if name == "copy" and isinstance(target, dict) and target.get(_OBJECT_TYPE_KEY):
            # User-defined types are values, so `.copy()` duplicates the fields
            # while keeping the type and its methods.
            return {**target}
        method = self.methods.get(name)
        if method is not None:
            return self._call_function(
                method,
                [target, *arguments],
                keywords or {},
                values if values is not None else self.values,
            )
        if isinstance(target, list):
            if name in {
                "rows",
                "columns",
                "row",
                "col",
                "add_row",
                "remove_row",
                "add_col",
                "remove_col",
                "swap_rows",
                "swap_columns",
                "flatten",
                "reshape",
            }:
                return self._matrix_method_call(name, [target, *arguments], keywords)
            if name == "mult" and target and isinstance(target[0], list) and arguments:
                # `matrix.mult()` scales by a scalar or multiplies by another
                # matrix of matching inner dimensions.
                if isinstance(arguments[0], list):
                    inner = arguments[0]
                    for row in range(len(target)):
                        for column in range(len(target[row])):
                            target[row][column] = sum(
                                self._number(target[row][index])
                                * self._number(inner[index][column])
                                for index in range(len(inner))
                            )
                    return target
                factor = self._number(arguments[0])
                for row in target:
                    for column in range(len(row)):
                        row[column] = self._number(row[column]) * factor
                return target
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
            if name == "slice" and arguments:
                start = int(self._number(arguments[0]))
                end = int(self._number(arguments[1])) if len(arguments) >= 2 else None
                return target[start:end]
            if name in {"binary_search_leftmost", "binary_search_rightmost"} and arguments:
                needle = self._number(arguments[0])
                low, high = 0, len(target)
                while low < high:
                    middle = (low + high) // 2
                    current = self._number(target[middle])
                    if name == "binary_search_leftmost":
                        if current < needle:
                            low = middle + 1
                        else:
                            high = middle
                    elif current <= needle:
                        low = middle + 1
                    else:
                        high = middle
                return low
            if name == "concat" and arguments and isinstance(arguments[0], list):
                return target + arguments[0]
        if isinstance(target, dict):
            if name == "get" and arguments:
                return target.get(self._map_key(arguments[0]))
            if name == "put" and len(arguments) >= 2:
                target[self._map_key(arguments[0])] = arguments[1]
                return arguments[1]
            if name in {"contains", "contains_key"} and arguments:
                return self._map_key(arguments[0]) in target
            if name == "remove" and arguments:
                return target.pop(self._map_key(arguments[0]), None)
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

    def _array_call(
        self,
        name: str,
        arguments: Sequence[Any],
        keywords: Mapping[str, Any] | None = None,
    ) -> Any:
        args = list(arguments)
        named = dict(keywords or {})
        if "id" in named:
            args.insert(0, named.pop("id"))
        if "size" in named and not args:
            args.append(named.pop("size"))
        if "initial_value" in named and len(args) < 2:
            args.append(named.pop("initial_value"))
        for key in ("index", "value", "order"):
            if key in named:
                args.append(named.pop(key))
        arguments = args
        if name in {
            "new",
            "new_float",
            "new_int",
            "new_string",
            "new_bool",
            "new_line",
            "new_color",
            "new_box",
            "new_label",
            "new_linefill",
            "new_table",
        }:
            if arguments and arguments[0] is None:
                self.approximations.add("array.na_constructor_empty")
                return []
            size = int(self._number(arguments[0])) if arguments else 0
            value = arguments[1] if len(arguments) > 1 else ("" if name == "new_string" else 0.0)
            return [value] * max(0, size)
        if name == "from":
            return (
                list(arguments[0])
                if arguments and isinstance(arguments[0], (list, tuple))
                else list(arguments)
            )
        if name in {"sum", "avg", "max", "min", "median", "percentile_nearest_rank"} and arguments:
            values = (
                [value for value in arguments[0] if value is not None]
                if isinstance(arguments[0], list)
                else []
            )
            numbers = [self._number(value) for value in values]
            if not numbers:
                return None if name != "sum" else 0.0
            if name == "sum":
                return sum(numbers)
            if name == "avg":
                return sum(numbers) / len(numbers)
            if name == "max":
                return max(numbers)
            if name == "min":
                return min(numbers)
            ordered = sorted(numbers)
            if name == "median":
                middle = len(ordered) // 2
                return (
                    ordered[middle]
                    if len(ordered) % 2
                    else (ordered[middle - 1] + ordered[middle]) / 2
                )
            index = min(
                len(ordered) - 1,
                max(0, int(self._number(arguments[1])) if len(arguments) > 1 else 0),
            )
            return ordered[index]
        if name == "fill" and len(arguments) >= 2 and isinstance(arguments[0], list):
            arguments[0][:] = [arguments[1]] * len(arguments[0])
            return arguments[0]
        if name == "clear" and arguments and isinstance(arguments[0], list):
            arguments[0].clear()
            return arguments[0]
        if name == "sort" and arguments and isinstance(arguments[0], list):
            if any(self._contains_missing(value) for value in arguments[0]):
                self.approximations.add("array.sort_na_preserved")
                return arguments[0]
            arguments[0].sort(key=lambda value: self._number(value))
            return arguments[0]
        if name == "remove" and len(arguments) >= 2 and isinstance(arguments[0], list):
            if self._contains_missing(arguments[1]):
                return arguments[0]
            index = int(self._number(arguments[1]))
            return (
                arguments[0].pop(index) if -len(arguments[0]) <= index < len(arguments[0]) else None
            )
        if name == "copy" and arguments and isinstance(arguments[0], list):
            return list(arguments[0])
        if (
            name == "concat"
            and len(arguments) >= 2
            and all(isinstance(value, list) for value in arguments[:2])
        ):
            return [*arguments[0], *arguments[1]]
        if name == "insert" and len(arguments) >= 3 and isinstance(arguments[0], list):
            if self._contains_missing(arguments[1]):
                return arguments[0]
            index = int(self._number(arguments[1]))
            if -len(arguments[0]) <= index <= len(arguments[0]):
                arguments[0].insert(index, arguments[2])
            return arguments[0]
        if name == "sort_indices" and arguments and isinstance(arguments[0], list):
            try:
                return sorted(
                    range(len(arguments[0])),
                    key=lambda index: self._number(arguments[0][index]),
                )
            except BacktestValidationError:
                return []
        if name in {"binary_search_leftmost", "binary_search_rightmost"} and len(arguments) >= 2:
            target = arguments[0]
            if self._contains_missing(arguments[1]):
                return None
            needle = self._number(arguments[1])
            if not isinstance(target, list):
                return None
            indices = [index for index, value in enumerate(target) if self._number(value) >= needle]
            if name.endswith("leftmost"):
                return indices[0] if indices else None
            return indices[-1] if indices else None
        if name == "size" and arguments:
            return len(arguments[0]) if isinstance(arguments[0], (list, tuple, str, dict)) else None
        if name == "first" and arguments:
            target = arguments[0]
            return target[0] if isinstance(target, list) and target else None
        if name == "last" and arguments:
            target = arguments[0]
            return target[-1] if isinstance(target, list) and target else None
        if name == "get" and len(arguments) > 1:
            target = arguments[0]
            if self._contains_missing(arguments[1]):
                return None
            index = int(self._number(arguments[1]))
            return (
                target[index]
                if isinstance(target, list) and -len(target) <= index < len(target)
                else None
            )
        if name == "set" and len(arguments) > 2 and isinstance(arguments[0], list):
            if self._contains_missing(arguments[1]):
                return arguments[0]
            index = int(self._number(arguments[1]))
            if -len(arguments[0]) <= index < len(arguments[0]):
                arguments[0][index] = arguments[2]
            return arguments[0]
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

    def _map_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if name in {"new", "new_string", "new_int", "new_float", "new_bool"}:
            return {}
        if not arguments or not isinstance(arguments[0], dict):
            return None
        target = arguments[0]
        if name == "size":
            return len(target)
        if name in {"get", "contains_key"} and len(arguments) >= 2:
            key = self._map_key(arguments[1])
            return key in target if name == "contains_key" else target.get(key)
        if name == "put" and len(arguments) >= 3:
            target[self._map_key(arguments[1])] = arguments[2]
            return target
        if name == "remove" and len(arguments) >= 2:
            return target.pop(self._map_key(arguments[1]), None)
        if name == "keys":
            return list(target)
        if name == "values":
            return list(target.values())
        if name == "clear":
            target.clear()
            return target
        return None

    @staticmethod
    def _map_key(value: Any) -> Any:
        try:
            hash(value)
        except TypeError:
            return str(value)
        return value

    def _string_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if not arguments or arguments[0] is None:
            return None
        value = str(arguments[0])
        if name == "tostring":
            if len(arguments) > 1 and arguments[1] is not None:
                try:
                    return format(value, str(arguments[1]))
                except (TypeError, ValueError):
                    return value
            return value
        if name == "length":
            return len(value)
        if name == "upper":
            return value.upper()
        if name == "lower":
            return value.lower()
        if name == "trim":
            return value.strip()
        if name == "contains" and len(arguments) > 1:
            return str(arguments[1]) in value
        if name in {"startswith", "endswith"} and len(arguments) > 1:
            return (
                value.startswith(str(arguments[1]))
                if name == "startswith"
                else value.endswith(str(arguments[1]))
            )
        if name in {"replace", "replace_all"} and len(arguments) > 2:
            return value.replace(str(arguments[1]), str(arguments[2]))
        if name == "split" and len(arguments) > 1:
            separator = str(arguments[1])
            return list(value) if not separator else value.split(separator)
        if name == "tonumber":
            try:
                return float(value)
            except ValueError:
                return None
        if name == "format" and len(arguments) > 1:
            template = value
            try:
                return template.format(*arguments[1:])
            except (IndexError, KeyError, ValueError):
                return template
        if name == "format_time" and len(arguments) > 1:
            timestamp = arguments[0]
            if isinstance(timestamp, (int, float)):
                timestamp = datetime.fromtimestamp(float(timestamp), UTC)
            if isinstance(timestamp, datetime):
                return timestamp.strftime(str(arguments[1]))
        if name == "substring" and len(arguments) > 2:
            start = max(0, int(self._number(arguments[1])))
            end = (
                len(value)
                if arguments[2] is None
                else min(len(value), int(self._number(arguments[2])))
            )
            return value[start:end]
        if name == "repeat" and len(arguments) > 1:
            if arguments[1] is None:
                return None
            return value * max(0, int(self._number(arguments[1])))
        if name == "reverse":
            return value[::-1]
        if name == "pos" and len(arguments) > 1:
            return value.find(str(arguments[1]))
        return None

    def _ohlcv_series(self, name: str) -> list[Any]:
        if self.current_candle is None:
            return []
        current = getattr(self.current_candle, name)
        return [*self.history.get(name, []), current]

    def _true_range_series(self) -> list[float]:
        cached = self._true_range_cache
        if cached is not None and cached[0] == self.current_index:
            return cached[1]
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
        self._true_range_cache = (self.current_index, ranges)
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

    def _current_vwap(self) -> float | None:
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

    def _current_obv(self) -> float:
        closes = self._ohlcv_series("close")
        volumes = self._ohlcv_series("volume")
        total = 0.0
        for index in range(1, len(closes)):
            if closes[index] is None or closes[index - 1] is None:
                continue
            close_delta = self._number(closes[index]) - self._number(closes[index - 1])
            total += self._number(volumes[index]) * (
                1 if close_delta > 0 else -1 if close_delta < 0 else 0
            )
        return total

    def _current_pvt(self) -> float:
        closes = self._ohlcv_series("close")
        volumes = self._ohlcv_series("volume")
        total = 0.0
        for index in range(1, len(closes)):
            previous = closes[index - 1]
            close = closes[index]
            volume = volumes[index]
            if previous in (None, 0) or close is None or volume is None:
                continue
            total += (
                self._number(volume)
                * (self._number(close) - self._number(previous))
                / self._number(previous)
            )
        return total

    def _current_accdist(self) -> float:
        """Chaikin accumulation/distribution line: cum(MFV)."""
        closes = self._ohlcv_series("close")
        highs = self._ohlcv_series("high")
        lows = self._ohlcv_series("low")
        volumes = self._ohlcv_series("volume")
        total = 0.0
        for index in range(len(closes)):
            high = highs[index] if index < len(highs) else None
            low = lows[index] if index < len(lows) else None
            close = closes[index]
            volume = volumes[index] if index < len(volumes) else None
            if high is None or low is None or close is None or volume is None:
                continue
            if self._number(high) == self._number(low):
                continue
            money_flow_multiplier = (
                (self._number(close) - self._number(low))
                - (self._number(high) - self._number(close))
            ) / (self._number(high) - self._number(low))
            total += money_flow_multiplier * self._number(volume)
        return total

    def _current_nvi(self) -> float:
        """Negative Volume Index: cumulative close change on down-volume bars."""
        closes = self._ohlcv_series("close")
        volumes = self._ohlcv_series("volume")
        nvi = 1000.0
        for index in range(1, len(closes)):
            previous_close = closes[index - 1]
            close = closes[index]
            volume = volumes[index] if index < len(volumes) else None
            previous_volume = volumes[index - 1] if index - 1 < len(volumes) else None
            if close is None or previous_close in (None, 0) or volume is None:
                continue
            if previous_volume is not None and self._number(volume) < self._number(previous_volume):
                nvi *= 1 + (self._number(close) - self._number(previous_close)) / self._number(
                    previous_close
                )
        return nvi

    def _ta_call(
        self,
        name: str,
        arguments: Sequence[Any],
        argument_nodes: Sequence[Expression],
        values: dict[str, Any],
        node: Node,
    ) -> Any:
        tuple_widths = {
            "linreg": 2,
            "aroon": 2,
            "supertrend": 2,
            "macd": 3,
            "bb": 3,
            "bbands": 3,
            "kc": 3,
            "donchian": 3,
            "dmi": 3,
        }
        if name == "stoch":
            # Pine Script v5 ta.stoch() returns scalar %K, not a tuple.
            name = "stoch_scalar"
        if name == "relativeVolume":
            # ta.relativeVolume(length, anchor, lookahead) -> [currentVolume, pastVolume, corr].
            # The anchor timeframe string is approximated away; we scale the last
            # bar's volume against the simple average over the lookback length.
            self.approximations.add("ta.relativeVolume")
            length = max(1, int(self._number(arguments[0]))) if arguments else 1
            volumes = [
                self._number(value)
                for value in self._ohlcv_series("volume")[-length:]
                if value is not None
            ]
            if not volumes:
                return [None, None, 0.0]
            past = sum(volumes) / len(volumes)
            current = self._last_number(self._ohlcv_series("volume"))
            if current is None:
                return [0.0, past, 0.0]
            return [current / past if past else 0.0, past, 0.0]
        if name == "donchian" and len(arguments) == 1:
            self.approximations.add("ta.legacy_default_source")
            length = int(self._number(arguments[0]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return [None, None, None]
            highs = [self._number(value) for value in self._ohlcv_series("high")[-length:]]
            lows = [self._number(value) for value in self._ohlcv_series("low")[-length:]]
            if not highs or not lows:
                return [None, None, None]
            upper, lower = max(highs), min(lows)
            return [(upper + lower) / 2, upper, lower]
        if name in {"highest", "lowest", "highestbars", "lowestbars"} and len(arguments) == 1:
            self.approximations.add("ta.legacy_default_source")
            if self._contains_missing(arguments[0]):
                return None
            length = int(self._number(arguments[0]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
            source_name = "high" if name in {"highest", "highestbars"} else "low"
            series = [
                self._number(value)
                for value in self._ohlcv_series(source_name)
                if value is not None
            ][-length:]
            if not series:
                return None
            target = max(series) if name in {"highest", "highestbars"} else min(series)
            return len(series) - 1 - series[::-1].index(target) if name.endswith("bars") else target
        if arguments and isinstance(arguments[0], (list, tuple)) and name not in tuple_widths:
            self.approximations.add("ta.tuple_series")
            return [
                self._ta_call(
                    name,
                    [element, *arguments[1:]],
                    (),
                    values,
                    node,
                )
                for element in arguments[0]
            ]
        if (
            arguments
            and any(value is None for value in arguments)
            and name
            in {
                "sma",
                "ema",
                "rma",
                "wma",
                "hma",
                "vwma",
                "rsi",
                "atr",
                "cci",
                "mfi",
                "wpr",
                "cmo",
                "macd",
                "stoch",
                "bb",
                "bbw",
                "bbands",
                "kc",
                "donchian",
                "supertrend",
                "sar",
                "adx",
                "plusdi",
                "minusdi",
                "highest",
                "lowest",
                "highestbars",
                "lowestbars",
                "change",
                "roc",
                "momentum",
                "linreg",
                "stdev",
                "variance",
                "mad",
                "cog",
                "correlation",
                "alma",
                "swma",
                "dmi",
            }
        ):
            width = tuple_widths.get(name)
            return [None] * width if width is not None else None
        if (
            arguments
            and any(value is None for value in arguments)
            and name
            not in {
                "crossover",
                "crossunder",
                "valuewhen",
                "barssince",
                "cum",
                "vwap",
                "obv",
                "tr",
            }
        ):
            self.approximations.add("ta.na_propagation")
            width = tuple_widths.get(name)
            return [None] * width if width is not None else None
        if name in {"crossover", "crossunder"} and len(arguments) > 1:
            if isinstance(arguments[0], (list, tuple)) or isinstance(arguments[1], (list, tuple)):
                self.approximations.add("tuple.cross")
                left_values = (
                    arguments[0] if isinstance(arguments[0], (list, tuple)) else [arguments[0]]
                )
                right_values = (
                    arguments[1]
                    if isinstance(arguments[1], (list, tuple))
                    else [arguments[1]] * len(left_values)
                )
                return [False for _ in zip(left_values, right_values, strict=False)]
            current_left = self._number(arguments[0]) if arguments[0] is not None else None
            current_right = self._number(arguments[1]) if arguments[1] is not None else None
            previous_left = (
                self._previous_number(argument_nodes[0], values)
                if len(argument_nodes) > 0
                else None
            )
            previous_right = (
                self._previous_number(argument_nodes[1], values)
                if len(argument_nodes) > 1
                else None
            )
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
                self.approximations.add("ta.invalid_length_na")
                return None
            ranges = self._true_range_series()
            return self._moving_average("rma", ranges, length)
        if name == "rsi":
            if len(arguments) < 2:
                return None
            length = int(self._number(arguments[1]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
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
                self.approximations.add("ta.invalid_length_na")
                return None
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
                self.approximations.add("ta.invalid_length_na")
                return None
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
        if name == "cmo":
            if len(arguments) >= 2:
                source = arguments[0]
                length = int(self._number(arguments[1]))
                source_node = argument_nodes[0] if argument_nodes else None
            else:
                source = self._last_number(self._ohlcv_series("close"))
                length = int(self._number(arguments[0])) if arguments else 14
                source_node = None
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
            closes = [
                value
                for value in (
                    self._series_values_for_value(source, values, source_node)
                    if source_node is not None
                    else [source]
                )
                if value is not None
            ]
            numbers = [self._number(value) for value in closes]
            if len(numbers) <= length:
                return None
            changes = [numbers[index] - numbers[index - 1] for index in range(1, len(numbers))]
            window = changes[-length:]
            upward = sum(change for change in window if change > 0)
            downward = -sum(change for change in window if change < 0)
            return (upward - downward) / (upward + downward) * 100 if upward + downward else 0.0
        if name == "wpr":
            length = int(self._number(arguments[0])) if arguments else 14
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
            highs = [self._number(value) for value in self._ohlcv_series("high")[-length:]]
            lows = [self._number(value) for value in self._ohlcv_series("low")[-length:]]
            closes = self._ohlcv_series("close")
            close = self._last_number(closes)
            if not highs or not lows or close is None or max(highs) == min(lows):
                return None
            return -100 * (max(highs) - close) / (max(highs) - min(lows))
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
            return self._current_vwap()
        if name in {"stoch", "stoch_scalar"} and len(arguments) >= 4:
            scalar = name == "stoch_scalar"
            length = int(self._number(arguments[3]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None if scalar else [None, None]
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
                return None if scalar else [None, None]
            highest = max(self._number(value) for value in high_series[-length:])
            lowest = min(self._number(value) for value in low_series[-length:])
            current = self._number(source_series[-1])
            percent_k = (current - lowest) / (highest - lowest) * 100 if highest != lowest else 50.0
            return percent_k if scalar else [percent_k, percent_k]
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
                self.approximations.add("ta.invalid_length_na")
                return None
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
                self.approximations.add("ta.invalid_length_na")
                return None
            window = series[-length:]
            if not window:
                return None
            target = max(window) if name == "highestbars" else min(window)
            return len(window) - 1 - window[::-1].index(target)
        if name == "tsi" and len(arguments) >= 3:
            self.approximations.add("ta.tsi")
            source = arguments[0]
            long_length = int(self._number(arguments[1]))
            short_length = int(self._number(arguments[2]))
            if long_length <= 0 or short_length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
            series = [
                value
                for value in self._series_values_for_value(
                    source, values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            if len(series) <= max(long_length, short_length):
                return None
            momentum_value = self._moving_average("ema", series, short_length)
            smoothed = self._ema_series(series, long_length)
            return smoothed[-1] if smoothed and momentum_value is not None else None
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
        if name in {"linreg", "linreg_scalar"} and len(arguments) >= 2:
            source_values = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            if any(isinstance(value, (list, tuple)) for value in source_values):
                self.approximations.add("ta.linreg_tuple_input_na")
                return None
            series = [self._number(value) for value in source_values if value is not None]
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
            if name == "linreg_scalar":
                offset = int(self._number(arguments[2])) if len(arguments) > 2 else 0
                intercept = y_mean - slope * x_mean
                return intercept + slope * (length - 1 - offset)
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
                self.approximations.add("ta.invalid_length_na")
                return None
            window = series[-length:]
            denominator = length * (length + 1) / 2
            return sum((index + 1) * value for index, value in enumerate(window)) / denominator
        if name == "aroon" and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return [None, None]
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
                self.approximations.add("ta.invalid_length_na")
                return None
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
        if name == "dmi" and len(arguments) >= 2:
            length = int(self._number(arguments[1]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return [None, None, None]
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            if len(closes) <= length + 1:
                return [None, None, None]
            ranges = self._true_range_series()
            dmi_plus_moves: list[float] = []
            dmi_minus_moves: list[float] = []
            for index in range(1, len(closes)):
                up = self._number(highs[index]) - self._number(highs[index - 1])
                down = self._number(lows[index - 1]) - self._number(lows[index])
                dmi_plus_moves.append(max(up, 0.0) if up > down else 0.0)
                dmi_minus_moves.append(max(down, 0.0) if down > up else 0.0)
            tr_average = self._moving_average("rma", ranges, length) or 0.0
            plus_average = self._moving_average("rma", dmi_plus_moves, length) or 0.0
            minus_average = self._moving_average("rma", dmi_minus_moves, length) or 0.0
            plus_di = 100 * plus_average / tr_average if tr_average else 0.0
            minus_di = 100 * minus_average / tr_average if tr_average else 0.0
            adx = (
                100 * abs(plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.0
            )
            return [plus_di, minus_di, adx]
        if name == "sar" and len(arguments) >= 2:
            self.approximations.add("ta.sar")
            step = self._number(arguments[0])
            maximum = self._number(arguments[1])
            if step <= 0 or maximum <= 0:
                self.approximations.add("ta.invalid_parameters_na")
                return None
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
        if name == "obv" and not arguments:
            return self._current_obv()
        if name == "iii" and not arguments:
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            volumes = self._ohlcv_series("volume")
            total = 0.0
            for index in range(len(closes)):
                bar_high = highs[index] if index < len(highs) else None
                bar_low = lows[index] if index < len(lows) else None
                close = closes[index]
                volume = volumes[index] if index < len(volumes) else None
                if bar_high is None or bar_low is None or close is None or volume is None:
                    continue
                if self._number(bar_high) == self._number(bar_low):
                    continue
                total += (
                    (2 * self._number(close) - self._number(bar_high) - self._number(bar_low))
                    / (self._number(bar_high) - self._number(bar_low))
                ) * self._number(volume)
            return total
        if name == "pvi" and not arguments:
            # Pressure Volume Index: each bar contributes
            # volume * (close - close[1]) / true_range, accumulated.
            closes = self._ohlcv_series("close")
            volumes = self._ohlcv_series("volume")
            true_ranges = self._true_range_series()
            total = 0.0
            for index in range(1, len(closes)):
                close = closes[index]
                previous = closes[index - 1]
                volume = volumes[index] if index < len(volumes) else None
                if close is None or previous is None or volume is None:
                    continue
                true_range = self._number(true_ranges[index]) if index < len(true_ranges) else 0.0
                if not true_range:
                    continue
                total += (
                    self._number(volume)
                    * (self._number(close) - self._number(previous))
                    / true_range
                )
            return total
        if name == "wad":
            # Williams Accumulation/Distribution: accumulate the close's move
            # against the prior bar's range, or a rolling sum when a length
            # is supplied.
            highs = self._ohlcv_series("high")
            lows = self._ohlcv_series("low")
            closes = self._ohlcv_series("close")
            contributions: list[float] = [0.0]
            for index in range(1, len(closes)):
                close = closes[index]
                previous = closes[index - 1]
                bar_low = lows[index] if index < len(lows) else None
                bar_high = highs[index] if index < len(highs) else None
                if close is None or previous is None or bar_low is None or bar_high is None:
                    contributions.append(0.0)
                    continue
                current = self._number(close)
                prior = self._number(previous)
                if current > prior:
                    contributions.append(current - min(self._number(bar_low), prior))
                elif current < prior:
                    contributions.append(current - max(self._number(bar_high), prior))
                else:
                    contributions.append(0.0)
            if not arguments:
                return sum(contributions)
            length = max(1, int(self._number(arguments[0])))
            return sum(contributions[-length:])
        if name == "wvad":
            # Williams Variable Accumulation/Distribution: the sum of the
            # close-to-close changes, accumulated or over a rolling length.
            closes = self._ohlcv_series("close")
            changes = [0.0]
            for index in range(1, len(closes)):
                close = closes[index]
                previous = closes[index - 1]
                if close is None or previous is None:
                    changes.append(0.0)
                    continue
                changes.append(self._number(close) - self._number(previous))
            if not arguments:
                return sum(changes)
            length = max(1, int(self._number(arguments[0])))
            return sum(changes[-length:])
        if not arguments:
            return None
        source = arguments[0]
        source_node = argument_nodes[0] if argument_nodes else None
        if len(arguments) > 1 and arguments[1] is not None and name != "anchored_vwap":
            try:
                raw_length = self._number(arguments[1])
            except BacktestValidationError:
                # Unknown ta.* function reached the generic MA-style coercion
                # with a non-numeric second argument (e.g. a timeframe anchor
                # string).  There is no implementation for it; surface an
                # explicit approximation instead of aborting the strategy.
                self.approximations.add(f"ta.{name}.approximation")
                return None
            length = int(raw_length)
        else:
            length = 1
        if length <= 0:
            self.approximations.add("ta.invalid_length_na")
            return None
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
            for high_value, low_value, close_value, volume_value in zip(
                highs[start_index:],
                lows[start_index:],
                closes[start_index:],
                volumes[start_index:],
                strict=False,
            ):
                if None in (high_value, low_value, close_value, volume_value):
                    continue
                typical = (
                    self._number(high_value) + self._number(low_value) + self._number(close_value)
                ) / 3
                numerator += typical * self._number(volume_value)
                denominator += self._number(volume_value)
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
        if name == "bbw":
            series = [
                self._number(value)
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            if len(series) < length:
                return None
            window = series[-length:]
            middle = sum(window) / length
            deviation = math.sqrt(sum((value - middle) ** 2 for value in window) / length)
            multiplier = self._number(arguments[2]) if len(arguments) > 2 else 2.0
            return 2 * multiplier * deviation / middle if middle else None
        if name in {"pivothigh", "pivotlow"} and len(arguments) >= 3:
            left_length = int(self._number(arguments[1]))
            right_length = int(self._number(arguments[2]))
            if left_length <= 0 or right_length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
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
                self.approximations.add("ta.invalid_length_na")
                return [None, None]
            ranges = self._true_range_series()
            average_range = self._moving_average("rma", ranges, atr_length)
            close = self._last_number(self._ohlcv_series("close"))
            if average_range is None or close is None:
                return [None, None]
            line = close - factor * average_range
            return [line, 1 if close >= line else -1]
        if name in {"dema", "tema", "trima", "frama"} and len(arguments) >= 2:
            self.approximations.add(f"ta.{name}")
            source = arguments[0]
            length = int(self._number(arguments[1]))
            if length <= 0:
                self.approximations.add("ta.invalid_length_na")
                return None
            series = [
                value
                for value in self._series_values_for_value(
                    source, values, argument_nodes[0] if argument_nodes else None
                )
                if value is not None
            ]
            first = self._ema_series(series, length)
            if name == "frama" or name == "trima":
                return self._moving_average("sma", series, length)
            second = self._ema_series(first, length)
            if name == "dema":
                return 2 * first[-1] - second[-1] if first and second else None
            third = self._ema_series(second, length)
            return (
                3 * first[-1] - 3 * second[-1] + third[-1] if first and second and third else None
            )
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
            # Pine Script: valuewhen(condition, source, occurrence)
            condition_values = self._series_values_for_value(
                arguments[0], values, argument_nodes[0] if argument_nodes else None
            )
            source_values = self._series_values_for_value(
                arguments[1], values, argument_nodes[1] if len(argument_nodes) > 1 else None
            )
            if self._contains_missing(arguments[2]):
                return None
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

    def _timeframe_call(self, name: str, arguments: Sequence[Any]) -> Any:
        timeframe = str(arguments[0]) if arguments else self.timeframe
        raw_timeframe = timeframe.strip()
        normalized = raw_timeframe.lower()
        if name == "change":
            self.approximations.add("timeframe.change")
            return False
        if name == "in_seconds":
            multiplier = normalized[:-1] if normalized and normalized[-1].isalpha() else normalized
            try:
                amount = float(multiplier)
            except ValueError:
                return 0
            unit = raw_timeframe[-1] if raw_timeframe and raw_timeframe[-1].isalpha() else "s"
            factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
            if unit == "M":
                return int(amount * 30 * 86400)
            return int(amount * factors.get(unit.lower(), 1))
        if name == "from_seconds" and arguments:
            try:
                seconds = int(self._number(arguments[0]))
            except BacktestValidationError:
                return None
            for suffix, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
                if seconds and seconds % size == 0:
                    return f"{seconds // size}{suffix}"
            return f"{seconds}s"
        if name in {"isdaily", "isweekly", "ismonthly", "isintraday", "isminutes", "isseconds"}:
            return self._member_timeframe_flag(name, timeframe)
        return None

    @staticmethod
    def _member_timeframe_flag(name: str, timeframe: str) -> bool:
        raw = timeframe.strip()
        normalized = raw.lower()
        if name == "isdaily":
            return normalized.endswith("d")
        if name == "isweekly":
            return normalized.endswith("w")
        if name == "ismonthly":
            return raw.endswith("M") or normalized.endswith("mo")
        if name == "isminutes":
            return normalized.endswith("m") and not raw.endswith("M")
        if name == "isseconds":
            return normalized.endswith("s")
        return not normalized.endswith(("d", "w", "mo")) and not raw.endswith("M")

    def _heikinashi_values(self) -> tuple[float, float, float, float]:
        candle = self.current_candle
        if candle is None:
            return (0.0, 0.0, 0.0, 0.0)
        current_close = (candle.open + candle.high + candle.low + candle.close) / 4
        previous = self.history.get("close", [])
        if previous:
            previous_close = self.history.get("open", [candle.open])[-1]
            previous_low = self.history.get("low", [candle.low])[-1]
            previous_high = self.history.get("high", [candle.high])[-1]
            previous_close_value = self.history.get("close", [candle.close])[-1]
            previous_ha_close = (
                previous_close + previous_high + previous_low + previous_close_value
            ) / 4
        else:
            previous_ha_close = current_close
        ha_open = (previous_ha_close + current_close) / 2
        return (
            ha_open,
            max(candle.high, ha_open),
            min(candle.low, ha_open),
            current_close,
        )

    @staticmethod
    def _contains_missing(value: Any) -> bool:
        # Iterative with cycle detection: tolerant scripts can build
        # self-referential containers (inexpressible in Pine), which must
        # degrade to diagnostics rather than RecursionError crashes.
        seen: set[int] = set()
        stack: list[Any] = [value]
        while stack:
            item = stack.pop()
            if item is None:
                return True
            if isinstance(item, (list, tuple)):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                stack.extend(item)
        return False

    def _rising_falling(
        self,
        name: str,
        arguments: Sequence[Any],
        argument_nodes: Sequence[Expression],
        values: dict[str, Any],
    ) -> bool:
        if len(argument_nodes) < 2:
            return False
        series = self._series_values_for_value(arguments[0], values, argument_nodes[0])
        length = max(1, int(self._number(arguments[1])))
        if len(series) < length + 1:
            return False
        recent = series[-(length + 1) :]
        if any(self._contains_missing(value) for value in recent):
            return False
        if name == "rising":
            return all(left < right for left, right in zip(recent, recent[1:], strict=False))
        return all(left > right for left, right in zip(recent, recent[1:], strict=False))

    def _time_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if name == "time" and arguments:
            session = next(
                (
                    value
                    for value in arguments
                    if isinstance(value, str) and ("-" in value or ":" in value)
                ),
                None,
            )
            if session and self.current_candle is not None:
                self.approximations.add("session.time")
                current = self.current_candle.timestamp
                start_text, _, remainder = session.partition("-")
                end_text = remainder.split(":", 1)[0] or start_text
                try:
                    start_minutes = int(start_text[:2]) * 60 + int(start_text[2:4])
                    end_minutes = int(end_text[:2]) * 60 + int(end_text[2:4])
                except ValueError:
                    start_minutes, end_minutes = 0, 24 * 60
                current_minutes = current.hour * 60 + current.minute
                in_session = (
                    start_minutes <= current_minutes < end_minutes
                    if start_minutes <= end_minutes
                    else current_minutes >= start_minutes or current_minutes < end_minutes
                )
                return int(current.timestamp()) if in_session else 0
        if (
            name == "time"
            and arguments
            and isinstance(arguments[0], str)
            and arguments[0] in {"D", "W", "M", "1D", "1W", "1M"}
            and self.current_candle is not None
        ):
            self.approximations.add("time.timeframe_approximation")
            return int(self.current_candle.timestamp.timestamp())
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
            return timestamp.isoweekday()
        if name == "weekofyear":
            return timestamp.timetuple().tm_yday
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
            if side == "long" and stop > limit:
                raise BacktestValidationError("long stop must be below limit")
            if side == "short" and stop < limit:
                raise BacktestValidationError("short stop must be above limit")
            if stop == limit:
                # Published scripts (e.g. touch/level entries) legitimately
                # place both legs at the same price.  TradingView's tester
                # accepts equality; treat it as a touch-level bracket instead
                # of aborting the whole backtest.
                self.approximations.add("order.stop_eq_limit")
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
            if quantity <= 0 or not math.isfinite(quantity):
                self.approximations.add("order.invalid_quantity_ignored")
                return 0.0
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
            if not math.isfinite(quantity) or quantity <= 0:
                self.approximations.add("order.invalid_quantity_ignored")
                return None
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
                accepted = self.broker.enter(
                    side,
                    quantity,
                    self.current_candle.close,
                    self.current_index,
                    self.current_candle.timestamp,
                )
                if not accepted:
                    self.approximations.add("order.rejected_or_ignored")
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
            self._validate_type_value(value, parameter.type_annotation, caller_values)
            local[parameter.name] = value
        old_values = self.values
        old_active_library = self._active_library
        self.values = local
        self._active_library = self._function_owner.get(id(function))
        try:
            result: Any = None
            for statement in function.body:
                result = self._execute_statement(statement, local)
            self._validate_type_value(result, function.return_type, local)
            return result
        except _Return as returned:
            self._validate_type_value(returned.value, function.return_type, local)
            return returned.value
        finally:
            self.values = old_values
            self._active_library = old_active_library

    def _dynamic_callee(
        self,
        expression: Expression,
        values: dict[str, Any],
        target: Any = _UNEVALUATED,
    ) -> tuple[Any, str] | None:
        """Resolve method calls whose receiver is itself a call expression."""

        if not isinstance(expression, MemberExpression):
            return None
        if target is _UNEVALUATED:
            try:
                target = self._evaluate(expression.object, values)
            except BacktestValidationError:
                return None
        if target is _MISSING:
            return None
        return target, expression.property

    def _callee_name(self, expression: Expression) -> str | None:
        key = id(expression)
        if key in self._callee_name_cache:
            return self._callee_name_cache[key]
        if isinstance(expression, (Identifier, NaLiteral)):
            result: str | None = "na" if isinstance(expression, NaLiteral) else expression.name
        elif isinstance(expression, MemberExpression):
            object_name = self._callee_name(expression.object)
            result = f"{object_name}.{expression.property}" if object_name else None
        else:
            result = None
        self._callee_name_cache[key] = result
        return result

    def _series_key(self, expression: Expression) -> str | None:
        key = id(expression)
        if key in self._series_key_cache:
            return self._series_key_cache[key]
        if isinstance(expression, Identifier):
            result: str | None = expression.name
        elif isinstance(expression, MemberExpression):
            parent = self._series_key(expression.object)
            result = f"{parent}.{expression.property}" if parent else None
        elif isinstance(expression, CallExpression):
            callee = self._callee_name(expression.callee)
            args = ",".join(
                self._series_key(argument) or "?"
                for argument in expression.arguments
                if isinstance(argument, Expression)
            )
            result = f"call:{callee}({args})" if callee else None
        elif isinstance(expression, HistoryExpression):
            result = self._series_key(expression.expression)
        else:
            result = None
        self._series_key_cache[key] = result
        return result

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

    def _number(self, value: Any) -> float:
        value_type = type(value)
        if value_type is float:
            return cast(float, value)
        if value_type is int:
            return float(value)
        if isinstance(value, bool):
            self.approximations.add("legacy.bool_numeric")
            return 1.0 if value else 0.0
        if isinstance(value, datetime):
            self.approximations.add("legacy.datetime_numeric")
            return value.timestamp()
        if not isinstance(value, (int, float)):
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
        if isinstance(value, (list, tuple)):
            # Iterative with cycle detection (see _contains_missing).
            seen: set[int] = {id(value)}
            stack: list[Any] = list(value)
            while stack:
                item = stack.pop()
                if item is None:
                    continue
                if isinstance(item, bool):
                    if item:
                        return True
                elif isinstance(item, (int, float)):
                    if item != 0:
                        return True
                elif isinstance(item, (list, tuple)):
                    if id(item) not in seen:
                        seen.add(id(item))
                        stack.extend(item)
                elif item:
                    return True
            return False
        return bool(value)

    @staticmethod
    def _equal(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        return bool(left == right)


@lru_cache(maxsize=4096)
def _parse_source_cached(source: str) -> Program:
    return Parser(Lexer(source).tokenize()).parse()


@lru_cache(maxsize=4096)
def _declares_library_title(path: Path, title: str) -> bool:
    """Report whether a Pine file declares `library("<title>")`."""

    return _library_title_pattern(title, True).search(_read_library_source(path)) is not None


@lru_cache(maxsize=4096)
def _declares_library_title_any_case(path: Path, title: str) -> bool:
    """Report whether a Pine file declares `library("<title>")`, ignoring case."""

    return _library_title_pattern(title, False).search(_read_library_source(path)) is not None


def _library_title_pattern(title: str, exact_case: bool) -> re.Pattern[str]:
    flags = 0 if exact_case else re.IGNORECASE
    return re.compile(rf'library\(\s*"{re.escape(title)}"\s*\)', flags)


def _read_library_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


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
