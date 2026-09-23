import pytest

from pine_interpreter import (
    ExecutionResult,
    Interpreter,
    Lexer,
    Parser,
    PineRuntimeError,
)


def execute(source: str) -> ExecutionResult:
    program = Parser(Lexer(source).tokenize()).parse()
    return Interpreter().execute(program)


def test_interpreter_evaluates_expressions_and_conditions() -> None:
    result = execute(
        """
threshold = 2 + 3
isReady = threshold > 4
label = "ready"

if isReady
    value = threshold * 2
else
    value = 0
"""
    )

    assert result.statements_executed == 5
    assert result.variables == {
        "threshold": 5,
        "isReady": True,
        "label": "ready",
    }


def test_interpreter_concatenates_strings() -> None:
    result = execute('prefix = "pine"\nvalue = prefix + " script"\n')

    assert result.variables["value"] == "pine script"


def test_interpreter_reports_division_by_zero() -> None:
    with pytest.raises(PineRuntimeError, match="division by zero"):
        execute("value = 1 / 0\n")


def test_interpreter_reports_unknown_identifier() -> None:
    with pytest.raises(PineRuntimeError, match="Unknown identifier"):
        execute("value = missing\n")
