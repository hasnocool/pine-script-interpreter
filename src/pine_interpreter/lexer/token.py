"""Token definitions used by the Pine lexer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from pine_interpreter.diagnostics import SourceLocation


class TokenType(Enum):
    """Lexical classes emitted for Pine source."""

    IDENTIFIER = auto()
    NUMBER = auto()
    STRING = auto()
    COLOR = auto()

    TRUE = auto()
    FALSE = auto()
    NA = auto()

    IF = auto()
    ELSE = auto()
    FOR = auto()
    WHILE = auto()
    TO = auto()
    BY = auto()
    IN = auto()
    BREAK = auto()
    CONTINUE = auto()
    SWITCH = auto()

    AND = auto()
    OR = auto()
    NOT = auto()

    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    DOUBLE_STAR = auto()

    EQUAL = auto()
    EQUAL_EQUAL = auto()
    BANG_EQUAL = auto()
    LESS = auto()
    LESS_EQUAL = auto()
    GREATER = auto()
    GREATER_EQUAL = auto()

    PLUS_EQUAL = auto()
    MINUS_EQUAL = auto()
    STAR_EQUAL = auto()
    SLASH_EQUAL = auto()
    PERCENT_EQUAL = auto()
    FAT_ARROW = auto()

    LEFT_PAREN = auto()
    RIGHT_PAREN = auto()
    LEFT_BRACKET = auto()
    RIGHT_BRACKET = auto()
    LEFT_BRACE = auto()
    RIGHT_BRACE = auto()
    DOT = auto()
    COMMA = auto()
    COLON = auto()
    COLON_EQUAL = auto()
    QUESTION = auto()
    SEMICOLON = auto()

    NEWLINE = auto()
    INDENT = auto()
    DEDENT = auto()
    EOF = auto()


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenType
    value: str
    location: SourceLocation

    def __str__(self) -> str:
        return f"{self.kind.name}({self.value!r}) at {self.location}"
