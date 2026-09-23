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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pine_interpreter.backtest.models import (
    BacktestConfig,
    BacktestExecutionLimitError,
    BacktestReport,
    BacktestValidationError,
    Candle,
    EquityPoint,
    Trade,
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
_NAMESPACE_NAMES = {
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


class _Broker:
    """Small market-order broker with close-price fills."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.cash = config.initial_cash
        self.position: _OpenPosition | None = None
        self.trades: list[Trade] = []

    def _fill_price(self, price: float, side: str) -> float:
        slippage = self.config.slippage_bps / 10_000
        return price * (1 + slippage if side == "long" else 1 - slippage)

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
                return
            self.close(price, index, timestamp, reason="reverse")
        fill = self._fill_price(price, side)
        fee = abs(quantity * fill) * self.config.fee_rate
        if side == "long":
            self.cash -= quantity * fill + fee
        else:
            self.cash += quantity * fill - fee
        if self.cash < -1e-9:
            raise BacktestValidationError("order exceeds available cash")
        self.position = _OpenPosition(side, quantity, fill, index, timestamp, fee)

    def close(
        self,
        price: float,
        index: int,
        timestamp: datetime,
        reason: str = "exit",
    ) -> Trade | None:
        if self.position is None:
            return None
        position = self.position
        fill = self._fill_price(price, position.side)
        fee = abs(position.quantity * fill) * self.config.fee_rate
        if position.side == "long":
            pnl = position.quantity * (fill - position.entry_price)
            self.cash += position.quantity * fill - fee
        else:
            pnl = position.quantity * (position.entry_price - fill)
            self.cash -= position.quantity * fill + fee
        pnl -= position.entry_fee + fee
        trade = Trade(
            position.entry_index,
            index,
            position.entry_timestamp,
            timestamp,
            position.side,
            position.quantity,
            position.entry_price,
            fill,
            pnl,
            position.entry_fee + fee,
            reason,
        )
        self.trades.append(trade)
        self.position = None
        return trade

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


@dataclass(slots=True)
class _PendingExit:
    stop: float | None
    limit: float | None
    from_entry: str | None


class _PineRuntime:
    """Evaluate one parsed Pine program against normalized candles."""

    def __init__(self, program: Program, config: BacktestConfig) -> None:
        self.program = program
        self.config = config
        self.broker = _Broker(config)
        self.values: dict[str, Any] = {}
        self.persistent: dict[str, Any] = {}
        self.persistent_names: set[str] = set()
        self.history: dict[str, list[Any]] = {}
        self.call_history: dict[str, list[Any]] = {}
        self.call_seen: set[str] = set()
        self.input_cache: dict[str, Any] = {}
        self.functions: dict[str, FunctionDeclaration] = {}
        self.enums: dict[str, dict[str, Any]] = {}
        self.current_candle: Candle | None = None
        self.current_index = -1
        self.execution_steps = 0
        self.pending_exits: dict[str, _PendingExit] = {}
        self._register_declarations()

    def _register_declarations(self) -> None:
        for statement in self.program.statements:
            if isinstance(statement, FunctionDeclaration):
                self.functions[statement.name] = statement
            elif isinstance(statement, EnumDeclaration):
                self.enums[statement.name] = self._enum_values(statement)

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
        peak = self.config.initial_cash
        for index, candle in enumerate(candles):
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
                "na": None,
            }
            self.values.update(self.persistent)
            for statement in self.program.statements:
                self._execute_statement(statement, self.values)
            equity = self.broker.equity(candle.close)
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
        return BacktestReport(
            name,
            symbol,
            timeframe,
            exchange,
            self.config.initial_cash,
            self.broker.equity(candles[-1].close) if candles else self.config.initial_cash,
            tuple(self.broker.trades),
            tuple(equity_curve),
            len(candles),
        )

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
        if stop_hit:
            self.broker.close(float(pending.stop or candle.close), index, candle.timestamp, "stop")
            self.pending_exits.pop(position.side, None)
        elif limit_hit:
            self.broker.close(
                float(pending.limit or candle.close), index, candle.timestamp, "limit"
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

    def _execute_statements(self, statements: Sequence[Statement], values: dict[str, Any]) -> Any:
        result: Any = None
        for statement in statements:
            result = self._execute_statement(statement, values)
        return result

    def _execute_statement(self, statement: Statement, values: dict[str, Any]) -> Any:
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
            return left_number**right_number
        raise BacktestValidationError(f"unsupported binary operator: {expression.operator}")

    def _member(self, expression: MemberExpression, values: dict[str, Any]) -> Any:
        if isinstance(expression.object, Identifier):
            name = expression.object.name
            if name in self.enums:
                enum = self.enums[name]
                return enum.get(expression.property, expression.property)
            if name == "strategy":
                return self._strategy_member(expression.property)
            if name in {
                "color",
                "currency",
                "format",
                "location",
                "shape",
                "size",
                "display",
                "plot",
                "barmerge",
                "hline",
                "position",
                "table",
            }:
                return expression.property
            if name == "math":
                return {"pi": math.pi, "e": math.e, "phi": (1 + math.sqrt(5)) / 2}.get(
                    expression.property
                )
            if name == "dayofweek":
                return expression.property
            if name == "source":
                return "close"
            if name == "resolution":
                return "1h"
            if name == "timeframe":
                return {
                    "isdaily": False,
                    "isintraday": True,
                    "isminutes": True,
                    "ismonthly": False,
                    "isweekly": False,
                }.get(expression.property)
            if name == "syminfo":
                return {"mintick": 0.01, "ticker": "UNKNOWN", "prefix": "", "currency": "USD"}.get(
                    expression.property
                )
            if name == "barstate":
                return {
                    "isconfirmed": True,
                    "isfirst": self.current_index == 0,
                    "islast": False,
                    "isrealtime": False,
                }.get(expression.property)
            if name == "session":
                return "regular"
            if name == "dayofweek":
                return expression.property
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
        if name == "flat":
            return "flat"
        if name == "position_size":
            if position is None:
                return 0.0
            return position.quantity if position.side == "long" else -position.quantity
        if name == "position_avg_price":
            return position.entry_price if position is not None else 0.0
        if name == "initial_capital":
            return self.config.initial_cash
        if name == "closedtrades":
            return len(self.broker.trades)
        if name == "equity":
            return self.broker.equity(price)
        if name == "netprofit":
            return self.broker.equity(price) - self.config.initial_cash
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
        return None

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

    def _call_named(
        self,
        name: str,
        arguments: list[Any],
        argument_nodes: Sequence[Expression],
        keywords: dict[str, Any],
        values: dict[str, Any],
        node: Node,
    ) -> Any:
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
        if name.startswith("strategy."):
            return self._strategy_call(name[9:], arguments, keywords, node)
        if name.startswith("input."):
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
        if name in {
            "timestamp",
            "time",
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
        function = self.functions.get(name)
        if function is not None:
            return self._call_function(function, arguments, keywords, values)
        return None

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
            return numbers[0] ** numbers[1]
        return numbers[0]

    def _array_call(self, name: str, arguments: Sequence[Any]) -> Any:
        if name in {"new", "new_float", "new_int", "new_string", "new_bool"}:
            size = int(self._number(arguments[0])) if arguments else 0
            value = arguments[1] if len(arguments) > 1 else (0.0 if name != "new_string" else "")
            return [value] * max(0, size)
        if name == "size" and arguments:
            return len(arguments[0])
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
            return value.split(str(arguments[1]))
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
        if not arguments:
            return None
        source = arguments[0]
        source_node = argument_nodes[0] if argument_nodes else None
        length = (
            int(self._number(arguments[1]))
            if len(arguments) > 1 and arguments[1] is not None
            else 1
        )
        if length <= 0:
            raise BacktestValidationError("ta.length must be positive")
        if name in {"sma", "ema", "rma", "wma", "hma"}:
            series = self._series_values_for_value(source, values, source_node)
            return self._moving_average(name, series, length)
        if name in {"highest", "lowest"}:
            series = [
                value
                for value in self._series_values_for_value(source, values, source_node)
                if value is not None
            ]
            return (max(series) if name == "highest" else min(series)) if series else None
        if name in {"change", "roc"}:
            series = self._series_values_for_value(source, values, source_node)
            if len(series) < 2 or series[-1] is None or series[-2] in (None, 0):
                return None
            change = self._number(series[-1]) - self._number(series[-2])
            return change if name == "change" else change / self._number(series[-2]) * 100
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
            current = series[-1] if series else None
            return sum(value <= current for value in series) / len(series) * 100 if series else None
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
        if name in {"timestamp", "time"}:
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
            quantity = self._number(keywords.get("qty", self.config.default_qty))
            self.broker.enter(
                side,
                quantity,
                self.current_candle.close,
                self.current_index,
                self.current_candle.timestamp,
            )
            return None
        if name == "close":
            if self._truthy(keywords.get("when", True)):
                self.broker.close(
                    self.current_candle.close,
                    self.current_index,
                    self.current_candle.timestamp,
                    "strategy.close",
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
                stop = self._optional_number(keywords.get("stop"))
                limit = self._optional_number(keywords.get("limit"))
                self.pending_exits[self.broker.position.side] = _PendingExit(
                    stop, limit, keywords.get("from_entry")
                )
            return None
        if name in {"cancel", "cancel_all"}:
            self.pending_exits.clear()
            return None
        return None

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
        self.values = local
        try:
            result: Any = None
            for statement in function.body:
                result = self._execute_statement(statement, local)
            return result
        except _Return as returned:
            return returned.value
        finally:
            self.values = old_values

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


class BacktestEngine:
    """Run a Pine source string against historical or live candles."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

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
        return Parser(Lexer(source).tokenize()).parse()

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
        runtime = _PineRuntime(program, self.config)
        return runtime.run(
            candle_list,
            name=name,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        )


# A short alias is convenient in notebooks and keeps the public API discoverable.
PineBacktester = BacktestEngine
