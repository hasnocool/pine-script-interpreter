"""Tree-walking evaluator for the initial Pine language subset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, TypeGuard

from pine_interpreter.diagnostics import PineRuntimeError
from pine_interpreter.parser.ast_nodes import (
    BinaryExpression,
    BooleanLiteral,
    CallExpression,
    Expression,
    ExpressionStatement,
    Identifier,
    IfStatement,
    Node,
    NumberLiteral,
    Program,
    Statement,
    StringLiteral,
    UnaryExpression,
    VariableDeclaration,
)
from pine_interpreter.runtime import RuntimeValue, Scope


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    statements_executed: int
    variables: dict[str, RuntimeValue]


class Interpreter:
    """Execute a parsed Pine program in a fresh root scope."""

    def execute(self, program: Program, parent: Scope | None = None) -> ExecutionResult:
        root = Scope(parent)
        executed = 0
        for statement in program.statements:
            executed += self._execute_statement(statement, root)
        return ExecutionResult(executed, root.local_values())

    def _execute_statement(self, statement: Statement, scope: Scope) -> int:
        if isinstance(statement, VariableDeclaration):
            scope.declare(statement.name, self._evaluate(statement.value, scope))
            return 1

        if isinstance(statement, ExpressionStatement):
            self._evaluate(statement.expression, scope)
            return 1

        if isinstance(statement, IfStatement):
            condition = self._evaluate(statement.condition, scope)
            if not isinstance(condition, bool):
                self._fail("if condition must evaluate to bool", statement)
            branch = statement.then_branch if condition else statement.else_branch
            branch_scope = scope.child()
            return 1 + sum(
                self._execute_statement(nested_statement, branch_scope)
                for nested_statement in branch
            )

        self._fail(f"Unsupported statement {type(statement).__name__}", statement)

    def _evaluate(self, expression: Expression, scope: Scope) -> RuntimeValue:
        if isinstance(expression, NumberLiteral):
            return expression.value
        if isinstance(expression, StringLiteral):
            return expression.value
        if isinstance(expression, BooleanLiteral):
            return expression.value
        if isinstance(expression, Identifier):
            try:
                return scope.get(expression.name)
            except KeyError:
                self._fail(f"Unknown identifier {expression.name!r}", expression)

        if isinstance(expression, UnaryExpression):
            return self._evaluate_unary(expression, scope)
        if isinstance(expression, BinaryExpression):
            return self._evaluate_binary(expression, scope)
        if isinstance(expression, CallExpression):
            self._fail("Function calls are not implemented yet", expression)

        self._fail(f"Unsupported expression {type(expression).__name__}", expression)

    def _evaluate_unary(
        self,
        expression: UnaryExpression,
        scope: Scope,
    ) -> RuntimeValue:
        value = self._evaluate(expression.operand, scope)
        if expression.operator == "+":
            if not self._is_number(value):
                self._fail("Expected a numeric value", expression)
            return value
        if expression.operator == "-":
            if not self._is_number(value):
                self._fail("Expected a numeric value", expression)
            return -value
        if expression.operator == "not":
            if not isinstance(value, bool):
                self._fail("not operand must evaluate to bool", expression)
            return not value
        self._fail(f"Unknown unary operator {expression.operator!r}", expression)

    def _evaluate_binary(
        self,
        expression: BinaryExpression,
        scope: Scope,
    ) -> RuntimeValue:
        left = self._evaluate(expression.left, scope)
        right = self._evaluate(expression.right, scope)
        operator = expression.operator

        if operator in {"and", "or"}:
            if not isinstance(left, bool) or not isinstance(right, bool):
                self._fail("Boolean operators require bool operands", expression)
            return left and right if operator == "and" else left or right

        if operator == "+" and isinstance(left, str) and isinstance(right, str):
            return left + right
        if operator in {"==", "!="}:
            equal = left == right
            return equal if operator == "==" else not equal
        if operator in {"<", "<=", ">", ">="}:
            if not self._is_number(left) or not self._is_number(right):
                self._fail("Ordering comparisons require numeric operands", expression)
            if operator == "<":
                return left < right
            if operator == "<=":
                return left <= right
            if operator == ">":
                return left > right
            return left >= right

        if not self._is_number(left) or not self._is_number(right):
            self._fail("Arithmetic requires numeric operands", expression)
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            if right == 0:
                self._fail("division by zero", expression)
            return left / right
        if operator == "%":
            if right == 0:
                self._fail("modulo by zero", expression)
            return left % right
        if operator == "**":
            return left**right

        self._fail(f"Unknown binary operator {operator!r}", expression)

    @staticmethod
    def _is_number(value: RuntimeValue) -> TypeGuard[int | float]:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _fail(message: str, node: Node) -> NoReturn:
        raise PineRuntimeError(message, node.location)
