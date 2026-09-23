"""Command-line interface for parsing and running Pine files."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pine_interpreter import __version__
from pine_interpreter.diagnostics import PineError
from pine_interpreter.interpreter import Interpreter
from pine_interpreter.lexer import Lexer
from pine_interpreter.parser import Parser


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pine-interpreter",
        description="Parse and optionally execute a Pine Script file.",
    )
    parser.add_argument("script", type=Path, help="path to a .pine source file")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="evaluate the parsed program and print its variables",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    try:
        source = arguments.script.read_text(encoding="utf-8")
    except OSError as error:
        print(f"error: could not read {arguments.script}: {error}")
        return 1

    try:
        tokens = Lexer(source, str(arguments.script)).tokenize()
        program = Parser(tokens).parse()
    except PineError as error:
        print(f"error: {error}")
        return 1

    if not arguments.execute:
        print(f"Parsed successfully: {len(program.statements)} top-level statement(s).")
        return 0

    try:
        result = Interpreter().execute(program)
    except PineError as error:
        print(f"runtime error: {error}")
        return 1

    print(f"Executed {result.statements_executed} statement(s).")
    print(result.variables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
