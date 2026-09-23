from pathlib import Path

from pine_interpreter import Lexer, Parser, Program
from pine_interpreter.parser.ast_nodes import (
    AssignmentStatement,
    BinaryExpression,
    CallExpression,
    EnumDeclaration,
    ForStatement,
    FunctionDeclaration,
    HistoryExpression,
    IfStatement,
    MemberExpression,
    SwitchExpression,
    TupleDeclaration,
    TypeDeclaration,
    VariableDeclaration,
)

EXAMPLE = Path(__file__).parents[1] / "examples" / "01_basics.pine"


def parse(source: str) -> Program:
    return Parser(Lexer(source).tokenize()).parse()


def test_parser_reads_example() -> None:
    program = parse(EXAMPLE.read_text(encoding="utf-8"))

    assert len(program.statements) == 4
    assert isinstance(program.statements[3], IfStatement)
    assert len(program.statements[3].then_branch) == 1
    assert len(program.statements[3].else_branch) == 1


def test_parser_uses_precedence() -> None:
    program = parse("result = 1 + 2 * 3\n")

    declaration = program.statements[0]
    assert isinstance(declaration, VariableDeclaration)
    expression = declaration.value
    assert isinstance(expression, BinaryExpression)
    assert expression.operator == "+"
    assert isinstance(expression.right, BinaryExpression)
    assert expression.right.operator == "*"


def test_parser_allows_eof_to_close_a_block() -> None:
    program = parse("if true\n    result = 1\n")

    assert len(program.statements) == 1
    assert isinstance(program.statements[0], IfStatement)


def test_parser_handles_nested_dedents_before_another_statement() -> None:
    program = parse("if true\n    if true\n        nested = 1\nfollowing = 2\n")

    assert len(program.statements) == 2
    outer_if = program.statements[0]
    assert isinstance(outer_if, IfStatement)
    assert isinstance(outer_if.then_branch[0], IfStatement)
    assert isinstance(program.statements[1], VariableDeclaration)


def test_parser_reads_typed_declarations_and_modern_expressions() -> None:
    program = parse(
        """
const string title = "Modern"
var float previous = na
float chosen = condition ? 1.0 :
    2.0
value := ta.highest(high, length)[1]
"""
    )

    typed = program.statements[1]
    assert isinstance(typed, VariableDeclaration)
    assert typed.type_annotation is not None
    assert typed.type_annotation.name == "float"
    chosen = program.statements[2]
    assert isinstance(chosen, VariableDeclaration)
    assert chosen.type_annotation is not None
    assignment = program.statements[3]
    assert isinstance(assignment, AssignmentStatement)
    assert isinstance(assignment.value, HistoryExpression)


def test_parser_reads_functions_types_enums_loops_and_switches() -> None:
    program = parse(
        """
export enum Mode
    Fast
    Slow = 2

export type State
    int count = na
    method value(State this) => this.count

collect(array<float> source) =>
    var float total = 0.0
    for item in source
        total += item
    result = switch total
        0 => na
        => total
    [total, result]

export collect(array<float> source) => collect(source)
"""
    )

    enum = program.statements[0]
    assert isinstance(enum, EnumDeclaration)
    assert enum.exported
    assert len(enum.members) == 2
    state = program.statements[1]
    assert isinstance(state, TypeDeclaration)
    assert state.exported
    assert isinstance(state.methods[0], FunctionDeclaration)
    function = program.statements[2]
    assert isinstance(function, FunctionDeclaration)
    loop = function.body[1]
    assert isinstance(loop, ForStatement)
    switch = function.body[2]
    assert isinstance(switch, VariableDeclaration)
    assert isinstance(switch.value, SwitchExpression)
    result = program.statements[3]
    assert isinstance(result, FunctionDeclaration)
    assert result.exported


def test_parser_reads_namespaces_generic_calls_and_tuple_destructuring() -> None:
    program = parse(
        """
values = array.new<float>(2, 0.0)
[first, second] = calculate.request(values)
"""
    )

    declaration = program.statements[0]
    assert isinstance(declaration, VariableDeclaration)
    assert isinstance(declaration.value, CallExpression)
    assert declaration.value.type_arguments
    tuple_declaration = program.statements[1]
    assert isinstance(tuple_declaration, TupleDeclaration)
    assert isinstance(tuple_declaration.value, CallExpression)
    assert isinstance(tuple_declaration.value.callee, MemberExpression)


def test_parser_handles_visual_wrapping_and_split_function_headers() -> None:
    program = parse(
        """
value = if condition
    -(amount)
else
    amount

f(first,
  second)
 =>
    first + second
"""
    )

    assert len(program.statements) == 2
    assert isinstance(program.statements[0], VariableDeclaration)
    assert isinstance(program.statements[1], FunctionDeclaration)


def test_parser_handles_comma_led_switch_case_bodies() -> None:
    program = parse(
        """
f() =>
    result = switch mode
        "a" => first := 1,
                 second := 2
        => fallback
"""
    )

    assert len(program.statements) == 1


def test_parser_handles_visual_switch_boundaries_and_return_expressions() -> None:
    program = parse(
        """
f() =>
    result = switch
        predicate(1) => first()
        => fallback()
    result
"""
    )

    assert len(program.statements) == 1
    assert isinstance(program.statements[0], FunctionDeclaration)
