"""Immutable Pine syntax-tree nodes."""

from __future__ import annotations

from dataclasses import dataclass

from pine_interpreter.diagnostics import SourceLocation


@dataclass(frozen=True, slots=True)
class Node:
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class NumberLiteral(Node):
    value: int | float


@dataclass(frozen=True, slots=True)
class StringLiteral(Node):
    value: str


@dataclass(frozen=True, slots=True)
class BooleanLiteral(Node):
    value: bool


@dataclass(frozen=True, slots=True)
class ColorLiteral(Node):
    value: str


@dataclass(frozen=True, slots=True)
class NaLiteral(Node):
    pass


@dataclass(frozen=True, slots=True)
class Identifier(Node):
    name: str


@dataclass(frozen=True, slots=True)
class TypeReference(Node):
    name: str
    arguments: tuple[TypeReference, ...] = ()


@dataclass(frozen=True, slots=True)
class UnaryExpression(Node):
    operator: str
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryExpression(Node):
    left: Expression
    operator: str
    right: Expression


@dataclass(frozen=True, slots=True)
class ConditionalExpression(Node):
    condition: Expression
    when_true: Expression
    when_false: Expression


@dataclass(frozen=True, slots=True)
class MemberExpression(Node):
    object: Expression
    property: str


@dataclass(frozen=True, slots=True)
class HistoryExpression(Node):
    expression: Expression
    offset: Expression


@dataclass(frozen=True, slots=True)
class CallArgument(Node):
    name: str | None
    value: Expression


@dataclass(frozen=True, slots=True)
class CallExpression(Node):
    callee: Expression
    arguments: tuple[Expression | CallArgument, ...]
    type_arguments: tuple[TypeReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ArrayLiteral(Node):
    elements: tuple[Expression, ...]


@dataclass(frozen=True, slots=True)
class BraceLiteral(Node):
    elements: tuple[Expression, ...]


@dataclass(frozen=True, slots=True)
class IfExpression(Node):
    condition: Expression
    then_branch: tuple[Statement, ...]
    else_branch: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class SwitchCase(Node):
    patterns: tuple[Expression, ...]
    body: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class SwitchExpression(Node):
    subject: Expression | None
    cases: tuple[SwitchCase, ...]


@dataclass(frozen=True, slots=True)
class ExpressionStatement(Node):
    expression: Expression


@dataclass(frozen=True, slots=True)
class VariableDeclaration(Node):
    name: str
    value: Expression
    type_annotation: TypeReference | None = None
    storage: str | None = None
    qualifier: str | None = None
    exported: bool = False


@dataclass(frozen=True, slots=True)
class AssignmentStatement(Node):
    target: Expression
    operator: str
    value: Expression


@dataclass(frozen=True, slots=True)
class TupleDeclaration(Node):
    targets: tuple[Expression, ...]
    value: Expression


@dataclass(frozen=True, slots=True)
class IfStatement(Node):
    condition: Expression
    then_branch: tuple[Statement, ...]
    else_branch: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class ForStatement(Node):
    variable: str | tuple[Expression, ...]
    start: Expression
    end: Expression | None
    step: Expression | None
    body: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class WhileStatement(Node):
    condition: Expression
    body: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class ForExpression(Node):
    variable: str | tuple[Expression, ...]
    start: Expression
    end: Expression | None
    step: Expression | None
    body: tuple[Statement, ...]


@dataclass(frozen=True, slots=True)
class BreakStatement(Node):
    pass


@dataclass(frozen=True, slots=True)
class ContinueStatement(Node):
    pass


@dataclass(frozen=True, slots=True)
class SwitchStatement(Node):
    subject: Expression | None
    cases: tuple[SwitchCase, ...]


@dataclass(frozen=True, slots=True)
class Parameter(Node):
    name: str
    type_annotation: TypeReference | None = None
    default: Expression | None = None
    qualifier: str | None = None


@dataclass(frozen=True, slots=True)
class FunctionDeclaration(Node):
    name: str
    parameters: tuple[Parameter, ...]
    body: tuple[Statement, ...]
    return_type: TypeReference | None = None
    exported: bool = False
    method: bool = False


@dataclass(frozen=True, slots=True)
class FieldDeclaration(Node):
    name: str
    type_annotation: TypeReference | None
    value: Expression | None = None


@dataclass(frozen=True, slots=True)
class TypeDeclaration(Node):
    name: str
    fields: tuple[FieldDeclaration, ...]
    methods: tuple[FunctionDeclaration, ...]
    exported: bool = False


@dataclass(frozen=True, slots=True)
class EnumMember(Node):
    name: str
    value: Expression | None = None


@dataclass(frozen=True, slots=True)
class EnumDeclaration(Node):
    name: str
    members: tuple[EnumMember, ...]
    exported: bool = False


@dataclass(frozen=True, slots=True)
class ImportDeclaration(Node):
    import_path: str
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class Program(Node):
    statements: tuple[Statement, ...]


Expression = (
    NumberLiteral
    | StringLiteral
    | BooleanLiteral
    | ColorLiteral
    | NaLiteral
    | Identifier
    | UnaryExpression
    | BinaryExpression
    | ConditionalExpression
    | MemberExpression
    | HistoryExpression
    | CallExpression
    | ArrayLiteral
    | BraceLiteral
    | IfExpression
    | SwitchExpression
    | ForExpression
)

Statement = (
    ExpressionStatement
    | VariableDeclaration
    | AssignmentStatement
    | TupleDeclaration
    | IfStatement
    | ForStatement
    | WhileStatement
    | BreakStatement
    | ContinueStatement
    | SwitchStatement
    | FunctionDeclaration
    | TypeDeclaration
    | EnumDeclaration
    | ImportDeclaration
)
