from pathlib import Path

from pine_interpreter import Lexer, Parser, Program
from pine_interpreter.parser.ast_nodes import (
    AssignmentStatement,
    BinaryExpression,
    CallExpression,
    EnumDeclaration,
    ExpressionStatement,
    ForStatement,
    FunctionDeclaration,
    HistoryExpression,
    Identifier,
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


def test_parser_keeps_parenthesized_result_after_comma_separated_declarations() -> None:
    program = parse(
        """
normalize(source, length) =>
    high = highest(source, length), low = lowest(source, length)
    (high - low) / (high + low)
"""
    )

    function = program.statements[0]
    assert isinstance(function, FunctionDeclaration)
    high, low, result = function.body
    assert isinstance(high, VariableDeclaration)
    assert isinstance(low, VariableDeclaration)
    assert isinstance(low.value, CallExpression)
    assert isinstance(low.value.callee, Identifier)
    assert isinstance(result, ExpressionStatement)


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


def test_parser_does_not_swallow_sibling_after_switch_function() -> None:
    program = parse(
        """
f1() =>
    switch "a"
        "a" => 1
        => 2
f2() =>
    42
"""
    )

    names = [
        statement.name
        for statement in program.statements
        if isinstance(statement, FunctionDeclaration)
    ]
    assert names == ["f1", "f2"]


def test_parser_keeps_comma_chained_inline_function_body_local() -> None:
    program = parse(
        """
f(x, p) => a = cum(x), (a - a[p]) / p
g(value) =>
    value * 2
"""
    )

    names = [
        statement.name
        for statement in program.statements
        if isinstance(statement, FunctionDeclaration)
    ]
    assert names == ["f", "g"]

    function = program.statements[0]
    assert isinstance(function, FunctionDeclaration)
    first, second = function.body
    assert isinstance(first, VariableDeclaration)
    assert first.name == "a"
    assert isinstance(second, ExpressionStatement)


def test_parser_does_not_adopt_a_dedented_else_from_an_outer_if() -> None:
    program = parse(
        """
if outer
    for index = 0 to 2
        if armed
            if hit
                dead = true
else
    fallback = 1
after = 2
"""
    )

    assert len(program.statements) == 2
    outer = program.statements[0]
    assert isinstance(outer, IfStatement)
    assert outer.else_branch is not None
    assert len(outer.else_branch) == 1
    after = program.statements[1]
    assert isinstance(after, VariableDeclaration)
    assert after.name == "after"


def test_parser_keeps_else_if_chains_at_the_outer_if_column() -> None:
    program = parse(
        """
grade(value) =>
    string label = str.upper(value)
    float size = 1.0
    if str.contains(label, "GOLD")
        size := 100.0
    else if str.contains(label, "SILVER")
        size := 5000.0
    else
        size := 1.0
    size
"""
    )

    assert len(program.statements) == 1
    function = program.statements[0]
    assert isinstance(function, FunctionDeclaration)
    branch = function.body[2]
    assert isinstance(branch, IfStatement)
    assert branch.else_branch is not None
    chained = branch.else_branch[0]
    assert isinstance(chained, IfStatement)
    assert chained.else_branch is not None
    assert isinstance(chained.else_branch[0], AssignmentStatement)


def test_parser_pairs_an_else_across_space_and_tab_indentation() -> None:
    # The archive mixes four-space and tab indentation inside the same `else`
    # ladder.  Both indent to the same visual column, so the `else` still
    # belongs to the space-indented `if` it follows.
    program = parse(
        "ma(smoothing) =>\n"
        '    if smoothing == "RMA"\n'
        "        rma()\n"
        "    else\n"
        '        if smoothing == "SMA"\n'
        "            sma()\n"
        "\t\telse\n"
        '\t\t\tif smoothing == "EMA"\n'
        "\t\t\t\tema()\n"
        "after = 1\n"
    )

    assert len(program.statements) == 2
    function = program.statements[0]
    assert isinstance(function, FunctionDeclaration)
    assert function.name == "ma"
    branch = function.body[0]
    assert isinstance(branch, IfStatement)
    assert branch.else_branch is not None
    chained = branch.else_branch[0]
    assert isinstance(chained, IfStatement)
    assert chained.else_branch is not None
    innermost = chained.else_branch[0]
    assert isinstance(innermost, IfStatement)
    assert not innermost.else_branch
    after = program.statements[1]
    assert isinstance(after, VariableDeclaration)
    assert after.name == "after"


def test_parser_keeps_a_statement_after_a_switch_inside_its_block() -> None:
    # The switch case block's dedent also ends the enclosing block unless the
    # switch parser balances only its own indent.  When it swallowed the extra
    # dedents the following `if` escaped to the top level, so variables the
    # switch assigned were no longer in scope.
    program = parse(
        """
for index = 0 to 3
    state = "armed"
    remove = false
    switch state
        "armed" =>
            if close > open
                remove := true
            else
                remove := true
        "idle" =>
            if volume > 0
                remove := true
    if remove
        closeAll := true
"""
    )

    assert len(program.statements) == 1
    loop = program.statements[0]
    assert isinstance(loop, ForStatement)
    assert [type(statement).__name__ for statement in loop.body] == [
        "VariableDeclaration",
        "VariableDeclaration",
        "SwitchStatement",
        "IfStatement",
    ]
    trailing = loop.body[-1]
    assert isinstance(trailing, IfStatement)
    assert isinstance(trailing.then_branch[0], AssignmentStatement)
