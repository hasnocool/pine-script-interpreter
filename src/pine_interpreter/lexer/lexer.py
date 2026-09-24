"""A line- and indentation-aware lexer for Pine Script."""

from __future__ import annotations

from collections.abc import Sequence

from pine_interpreter.diagnostics import PineSyntaxError, SourceLocation
from pine_interpreter.lexer.token import Token, TokenType

_KEYWORDS: dict[str, TokenType] = {
    "and": TokenType.AND,
    "else": TokenType.ELSE,
    "false": TokenType.FALSE,
    "for": TokenType.FOR,
    "if": TokenType.IF,
    "na": TokenType.NA,
    "not": TokenType.NOT,
    "or": TokenType.OR,
    "true": TokenType.TRUE,
    "while": TokenType.WHILE,
}

# Keep this ordered: the lexer uses longest-match semantics.
_SYMBOLS: dict[str, TokenType] = {
    "**": TokenType.DOUBLE_STAR,
    "==": TokenType.EQUAL_EQUAL,
    "!=": TokenType.BANG_EQUAL,
    "<=": TokenType.LESS_EQUAL,
    ">=": TokenType.GREATER_EQUAL,
    "=>": TokenType.FAT_ARROW,
    "+=": TokenType.PLUS_EQUAL,
    "-=": TokenType.MINUS_EQUAL,
    "*=": TokenType.STAR_EQUAL,
    "/=": TokenType.SLASH_EQUAL,
    "%=": TokenType.PERCENT_EQUAL,
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "/": TokenType.SLASH,
    "%": TokenType.PERCENT,
    "=": TokenType.EQUAL,
    "<": TokenType.LESS,
    ">": TokenType.GREATER,
    "(": TokenType.LEFT_PAREN,
    ")": TokenType.RIGHT_PAREN,
    "[": TokenType.LEFT_BRACKET,
    "]": TokenType.RIGHT_BRACKET,
    "{": TokenType.LEFT_BRACE,
    "}": TokenType.RIGHT_BRACE,
    ".": TokenType.DOT,
    ",": TokenType.COMMA,
    ":=": TokenType.COLON_EQUAL,
    ":": TokenType.COLON,
    "?": TokenType.QUESTION,
    ";": TokenType.SEMICOLON,
}

_OPENING_DELIMITERS = {
    "(": ")",
    "[": "]",
    "{": "}",
}
_CLOSING_DELIMITERS = {value: key for key, value in _OPENING_DELIMITERS.items()}

_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

# Pine permits visual wrapping after these expression tokens. Such a line's
# indentation is formatting, not a new block.
_VALUE_NAMESPACES = {
    "array",
    "barstate",
    "box",
    "chart",
    "color",
    "currency",
    "dayofweek",
    "display",
    "format",
    "input",
    "label",
    "line",
    "map",
    "math",
    "matrix",
    "position",
    "request",
    "session",
    "size",
    "str",
    "strategy",
    "syminfo",
    "table",
    "ta",
    "text",
    "timeframe",
}

_TYPE_LINE_WORDS = {
    "array",
    "bool",
    "box",
    "chart.point",
    "color",
    "const",
    "float",
    "input",
    "int",
    "label",
    "line",
    "map",
    "matrix",
    "series",
    "simple",
    "string",
    "table",
    "var",
    "varip",
}

_CONTINUATION_ENDINGS = {
    TokenType.AND,
    TokenType.OR,
    TokenType.PLUS,
    TokenType.MINUS,
    TokenType.STAR,
    TokenType.SLASH,
    TokenType.PERCENT,
    TokenType.DOUBLE_STAR,
    TokenType.EQUAL,
    TokenType.COLON_EQUAL,
    TokenType.EQUAL_EQUAL,
    TokenType.BANG_EQUAL,
    TokenType.LESS,
    TokenType.LESS_EQUAL,
    TokenType.GREATER,
    TokenType.GREATER_EQUAL,
    TokenType.PLUS_EQUAL,
    TokenType.MINUS_EQUAL,
    TokenType.STAR_EQUAL,
    TokenType.SLASH_EQUAL,
    TokenType.PERCENT_EQUAL,
    TokenType.DOT,
    TokenType.QUESTION,
    TokenType.COLON,
    TokenType.NOT,
}


class Lexer:
    """Convert Pine source text into a token stream.

    Pine has no explicit end-of-line token. Its indentation is significant, but
    line breaks inside ``()``, ``[]``, and ``{}`` are continuation lines and do
    not participate in indentation processing.
    """

    def __init__(self, source: str, filename: str = "<string>") -> None:
        self._source = source
        self._filename = filename
        self._index = 0
        self._line = 1
        self._column = 1
        self._indents = [0]
        self._delimiters: list[tuple[str, SourceLocation]] = []
        self._at_line_start = True
        self._continuation_line = False
        self._continuation_operand = False
        self._continuation_indent = 0
        self._continuation_operator: str | None = None
        self._continuation_operator_leading = False
        self._current_line_indent = 0
        self._previous_line_control = False
        self._line_follows_control = False
        self._tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        """Tokenize the complete source input."""

        while not self._is_eof():
            if self._at_line_start:
                self._read_indentation()
                self._at_line_start = False
                continue

            character = self._peek()
            if character in "\r\n":
                self._read_line_break()
            elif character.isspace():
                self._advance()
            elif self._source.startswith("//", self._index):
                self._skip_comment()
            elif character in {'"', "'"} and self._source.startswith(character * 3, self._index):
                self._read_multiline_string(character)
            elif character in {'"', "'"}:
                self._read_string(character)
            elif character == "#" and self._is_color_literal():
                self._read_color()
            elif character.isdigit() or (character == "." and self._peek(1).isdigit()):
                self._read_number()
            elif self._is_identifier_start(character):
                self._read_identifier()
            else:
                self._read_symbol()

        self._finish()
        return self._tokens

    def _location(self) -> SourceLocation:
        return SourceLocation(self._filename, self._line, self._column)

    def _is_eof(self) -> bool:
        return self._index >= len(self._source)

    def _peek(self, offset: int = 0) -> str:
        index = self._index + offset
        if index >= len(self._source):
            return ""
        return self._source[index]

    def _advance(self) -> str:
        if self._is_eof():
            return ""
        character = self._source[self._index]
        self._index += 1
        if character == "\n":
            self._line += 1
            self._column = 1
        elif character == "\r" and self._peek() != "\n":
            self._line += 1
            self._column = 1
        elif character == "\t":
            # A tab advances the visual column by four so the reported column
            # matches the indent width `_read_indentation` computes.  Mixing
            # tabs and spaces otherwise makes indentation-sensitive parser
            # decisions (e.g. which `if` an `else` belongs to) disagree with
            # the indent stack.
            self._column += 4
        else:
            self._column += 1
        return character

    @staticmethod
    def _is_identifier_start(character: str) -> bool:
        return character == "_" or character.isalpha()

    @staticmethod
    def _is_identifier_part(character: str) -> bool:
        return character == "_" or character.isalnum()

    def _append(self, kind: TokenType, value: str, location: SourceLocation) -> None:
        self._tokens.append(Token(kind, value, location))

    def _read_indentation(self) -> None:
        width = 0
        while self._peek().isspace() and self._peek() not in {"\r", "\n"}:
            character = self._advance()
            width += 4 if character == "\t" else 1

        # Wrapped expression lines may use visual indentation of their own.
        if self._delimiters:
            return

        if self._is_eof() or self._peek() in {"\r", "\n"}:
            return
        if self._source.startswith("//", self._index):
            return
        self._line_follows_control = self._previous_line_control
        self._previous_line_control = self._line_starts_control_header()

        if width > 0 and self._previous_line_is_method_modifier():
            self._current_line_indent = width
            return

        if (
            width > 0
            and not self._line_follows_control
            and self._line_starts_statement()
            and self._previous_significant_value() in _TYPE_LINE_WORDS
        ):
            self._current_line_indent = width
            return

        if (
            self._peek() == "("
            and not self._line_follows_control
            and self._previous_significant_kind()
            in {
                TokenType.IDENTIFIER,
                TokenType.RIGHT_PAREN,
                TokenType.RIGHT_BRACKET,
            }
        ):
            while self._tokens and self._tokens[-1].kind is TokenType.NEWLINE:
                self._tokens.pop()
            self._current_line_indent = width
            return

        if (
            self._peek() == "="
            and self._peek(1) == ">"
            and self._previous_significant_kind() is TokenType.RIGHT_PAREN
        ):
            self._current_line_indent = width
            return

        if (
            self._peek() == "="
            and self._peek(1) == ">"
            and width > self._indents[-1]
            and self._previous_significant_kind() is not TokenType.RIGHT_PAREN
        ):
            while self._tokens and self._tokens[-1].kind is TokenType.NEWLINE:
                self._tokens.pop()
            self._continuation_line = True
            self._continuation_indent = max(width, self._indents[-1] + 1)
            self._continuation_operator = "=>"
            self._continuation_operator_leading = True
            self._current_line_indent = width
            return

        if self._peek() == ",":
            while self._tokens and self._tokens[-1].kind is TokenType.NEWLINE:
                self._tokens.pop()
            self._continuation_line = True
            self._continuation_indent = max(width, self._indents[-1] + 1)
            self._continuation_operator = ","
            self._continuation_operator_leading = True
            self._current_line_indent = width
            return

        if self._starts_operator_line() and not self._source.startswith("//", self._index):
            # Pine also permits operator-leading wrapping. Remove the physical
            # newline(s), then carry continuation state across this line.
            while self._tokens and self._tokens[-1].kind is TokenType.NEWLINE:
                self._tokens.pop()
            self._continuation_line = True
            self._continuation_indent = max(width, self._indents[-1] + 1)
            self._continuation_operator = self._operator_text()
            self._continuation_operator_leading = True
            self._current_line_indent = width
            return

        if (
            self._line_follows_control
            and not self._starts_operator_line()
            and not self._continuation_line
        ):
            self._current_line_indent = width
            location = self._location()
            if width > self._indents[-1]:
                self._append(TokenType.INDENT, "<indent>", location)
                self._indents.append(width)
            elif width < self._indents[-1]:
                while self._indents[-1] > width:
                    self._append(TokenType.DEDENT, "<dedent>", location)
                    self._indents.pop()
            return

        if self._continuation_line:
            # Keep the continuation pending across blank and comment-only lines.
            if self._is_eof() or self._peek() in {"\r", "\n"}:
                return
            if self._source.startswith("//", self._index):
                return
            self._continuation_line = False
            same_visual_continuation = (
                width > 0
                and width == self._indents[-1]
                and width == self._current_line_indent
                and not (
                    self._continuation_operator in {"and", "or", "to", "by"}
                    and self._line_starts_statement()
                )
                and not (
                    self._continuation_operator in {"?", ":"} and self._line_starts_statement()
                )
            )
            wrapped_body = width > self._indents[-1] and (
                self._continuation_operator_leading
                and self._continuation_operator in {"and", "or", "to", "by"}
                and self._line_starts_statement()
                or self._continuation_operator in {"?", ":"}
                and self._line_starts_assignment_or_control()
            )
            dedented_ternary_operand = (
                self._continuation_operator in {"?", ":"}
                and width > 0
                and not self._line_starts_assignment_or_control()
            )
            dedented_logical_operand = (
                self._continuation_operator in {"and", "or", "to", "by"}
                and width > 0
                and (self._peek() in {"(", "[", "-", "+"} or not self._line_starts_statement())
                and not self._line_starts_assignment_or_control()
            )
            if (
                width < self._indents[-1]
                and not dedented_ternary_operand
                and not dedented_logical_operand
                or width == self._indents[-1]
                and not same_visual_continuation
                and not dedented_ternary_operand
                or wrapped_body
            ):
                # The wrapped expression ended before this line. If this is a
                # continuation of a control-flow condition, the deeper line
                # is the body and must retain an indent marker.
                self._append(TokenType.NEWLINE, "\\n", self._location())
                self._continuation_operator = None
            else:
                self._continuation_operand = True
                self._current_line_indent = width
                return

        # Empty and comment-only lines do not affect indentation.
        if self._is_eof() or self._peek() in {"\r", "\n"}:
            return
        if self._source.startswith("//", self._index):
            return

        self._current_line_indent = width
        location = self._location()
        if width > self._indents[-1]:
            self._append(TokenType.INDENT, "<indent>", location)
            self._indents.append(width)
            return

        if width < self._indents[-1]:
            while self._indents[-1] > width:
                self._append(TokenType.DEDENT, "<dedent>", location)
                self._indents.pop()
            # A number of older public scripts mix visual continuation
            # indentation with block indentation. Snap such a dedent to the
            # nearest enclosing block rather than rejecting the whole file.
            self._append(TokenType.NEWLINE, "\\n", location)

    def _line_starts_control_header(self) -> bool:
        if self._operator_text() in {"if", "else", "for", "while", "switch"}:
            return True
        line_end = self._source.find("\n", self._index)
        if line_end < 0:
            line_end = len(self._source)
        compact = "".join(self._source[self._index : line_end].split())
        return "=if" in compact

    def _line_starts_assignment_or_control(self) -> bool:
        if self._peek() == "[":
            return True
        word = self._operator_text()
        if word in {"if", "else", "for", "while", "switch", "var", "varip", "const"}:
            return True
        end = self._index + len(word)
        line_end = self._source.find("\n", end)
        if line_end < 0:
            line_end = len(self._source)
        rest = self._source[end:line_end].lstrip()
        return rest.startswith((":=", "+=", "-=", "*=", "/=", "%=")) or (
            rest.startswith("=") and not rest.startswith("==")
        )

    def _line_starts_statement(self) -> bool:
        if self._peek().isdigit() or (self._peek() == "." and self._peek(1).isdigit()):
            return False
        if self._peek() == "[":
            return True
        word = self._operator_text()
        if word in {"if", "else", "for", "while", "switch", "var", "varip", "const"}:
            return True
        end = self._index + len(word)
        line_end = self._source.find("\n", end)
        if line_end < 0:
            line_end = len(self._source)
        rest = self._source[end:line_end].lstrip()
        if (rest.startswith(".") and "(" in rest and word not in _VALUE_NAMESPACES) or (
            self._is_identifier_start(self._peek()) and rest.startswith("(")
        ):
            return True
        return rest.startswith((":=", "+=", "-=", "*=", "/=", "%=")) or (
            rest.startswith("=") and not rest.startswith("==")
        )

    def _previous_line_is_method_modifier(self) -> bool:
        current_start = self._source.rfind("\n", 0, self._index) + 1
        previous_end = current_start - 1
        previous_start = self._source.rfind("\n", 0, previous_end) + 1
        previous = self._source[previous_start:previous_end].split("//", 1)[0].strip()
        return previous in {"method", "export method"}

    def _previous_significant_kind(self) -> TokenType:
        for token in reversed(self._tokens):
            if token.kind is not TokenType.NEWLINE:
                return token.kind
        return TokenType.EOF

    def _previous_significant_value(self) -> str:
        for token in reversed(self._tokens):
            if token.kind is not TokenType.NEWLINE:
                return token.value
        return ""

    def _previous_token_can_continue_expression(self) -> bool:
        for token in reversed(self._tokens):
            if token.kind is TokenType.NEWLINE:
                continue
            return token.kind in _CONTINUATION_ENDINGS or token.kind in {
                TokenType.COMMA,
                TokenType.RIGHT_PAREN,
                TokenType.RIGHT_BRACKET,
                TokenType.NUMBER,
                TokenType.STRING,
                TokenType.COLOR,
                TokenType.IDENTIFIER,
                TokenType.TRUE,
                TokenType.FALSE,
                TokenType.NA,
            }
        return False

    def _operator_text(self) -> str:
        character = self._peek()
        if not self._is_identifier_start(character):
            return character
        end = self._index + 1
        while self._source[end : end + 1] and self._is_identifier_part(self._source[end : end + 1]):
            end += 1
        return self._source[self._index : end]

    def _starts_operator_line(self) -> bool:
        character = self._peek()
        if character == "=" and self._peek(1) == ">":
            return False
        if character in "+-" and (self._peek(1).isdigit() or self._peek(1) == "."):
            if self._peek(1) != "." or self._peek(2).isdigit():
                return False
        if character == "." and self._peek(1).isdigit():
            return False
        if character in "+-":
            return not self._line_follows_control and self._previous_token_can_continue_expression()
        if character in "?:*/%=<>.":
            return True
        if not self._is_identifier_start(character):
            return False
        end = self._index + 1
        while self._source[end : end + 1] and self._is_identifier_part(self._source[end : end + 1]):
            end += 1
        return self._source[self._index : end] in {"and", "or", "to", "by"}

    def _last_token_continues(self) -> bool:
        if not self._tokens:
            return False
        token = self._tokens[-1]
        return token.kind in _CONTINUATION_ENDINGS or (
            token.kind is TokenType.IDENTIFIER and token.value in {"to", "by"}
        )

    def _read_line_break(self) -> None:
        location = self._location()
        character = self._advance()
        if character == "\r" and self._peek() == "\n":
            self._advance()
        if not self._delimiters:
            if self._continuation_line:
                self._continuation_operand = False
                if self._last_token_continues():
                    self._continuation_line = True
                    self._continuation_operator = self._tokens[-1].value
                    self._continuation_operator_leading = False
                    self._continuation_indent = max(
                        self._current_line_indent,
                        self._indents[-1] + 1,
                    )
                else:
                    self._continuation_line = False
                    self._continuation_operator = None
                    self._append(TokenType.NEWLINE, "\\n", location)
            elif self._continuation_operand:
                self._continuation_operand = False
                if self._last_token_continues():
                    self._continuation_line = True
                    self._continuation_operator = self._tokens[-1].value
                    self._continuation_operator_leading = False
                    self._continuation_indent = max(
                        self._current_line_indent,
                        self._indents[-1] + 1,
                    )
                else:
                    self._continuation_operator = None
                    self._append(TokenType.NEWLINE, "\\n", location)
            elif self._last_token_continues():
                self._continuation_line = True
                self._continuation_operator = self._tokens[-1].value
                self._continuation_operator_leading = False
                self._continuation_indent = max(
                    self._current_line_indent,
                    self._indents[-1] + 1,
                )
            else:
                self._append(TokenType.NEWLINE, "\\n", location)
        self._at_line_start = True

    def _skip_comment(self) -> None:
        while not self._is_eof() and self._peek() not in {"\r", "\n"}:
            self._advance()

    def _read_number(self) -> None:
        location = self._location()
        start = self._index

        if self._peek() == "0" and self._peek(1) in {"x", "X"}:
            self._advance()
            self._advance()
            digit_start = self._index
            while self._peek().isalnum():
                self._advance()
            if self._index == digit_start:
                raise PineSyntaxError("Hexadecimal literal has no digits", location)
            text = self._source[start : self._index]
            self._append(TokenType.NUMBER, text, location)
            return

        while self._peek().isdigit():
            self._advance()

        if self._peek() == "." and (
            self._peek(1).isdigit()
            or not self._is_identifier_start(self._peek(1))
            and self._peek(1) != "."
        ):
            self._advance()
            while self._peek().isdigit():
                self._advance()

        if self._peek() in {"e", "E"}:
            exponent_offset = 1
            if self._peek(exponent_offset) in {"+", "-"}:
                exponent_offset += 1
            if self._peek(exponent_offset).isdigit():
                self._advance()
                if self._peek() in {"+", "-"}:
                    self._advance()
                while self._peek().isdigit():
                    self._advance()

        self._append(TokenType.NUMBER, self._source[start : self._index], location)

    def _read_identifier(self) -> None:
        location = self._location()
        start = self._index
        self._advance()
        while self._is_identifier_part(self._peek()):
            self._advance()

        value = self._source[start : self._index]
        self._append(_KEYWORDS.get(value, TokenType.IDENTIFIER), value, location)

    def _is_color_literal(self) -> bool:
        digits = self._source[self._index + 1 : self._index + 7]
        return len(digits) == 6 and all(
            character in "0123456789abcdefABCDEF" for character in digits
        )

    def _read_color(self) -> None:
        location = self._location()
        self._advance()
        value_start = self._index
        while self._peek() and self._peek() in "0123456789abcdefABCDEF":
            self._advance()
        value = self._source[value_start : self._index]
        if len(value) not in {6, 8}:
            raise PineSyntaxError("Color literals must contain 6 or 8 hexadecimal digits", location)
        self._append(TokenType.COLOR, value, location)

    def _read_multiline_string(self, quote: str) -> None:
        location = self._location()
        delimiter = quote * 3
        for _ in delimiter:
            self._advance()
        characters: list[str] = []

        while not self._is_eof():
            if self._source.startswith(delimiter, self._index):
                for _ in delimiter:
                    self._advance()
                self._append(TokenType.STRING, "".join(characters), location)
                return

            character = self._advance()
            if character != "\\":
                characters.append(character)
                continue
            if self._is_eof():
                raise PineSyntaxError("Unterminated escape sequence", location)
            escaped = self._advance()
            characters.append(_ESCAPES.get(escaped, escaped))

        raise PineSyntaxError("Unterminated multiline string literal", location)

    def _read_string(self, quote: str) -> None:
        location = self._location()
        self._advance()
        characters: list[str] = []

        while True:
            if self._is_eof():
                raise PineSyntaxError("Unterminated string literal", location)
            if self._peek() in "\r\n":
                if self._peek() == "\r":
                    self._advance()
                    if self._peek() == "\n":
                        self._advance()
                else:
                    self._advance()
                characters.append("\n")
                continue
            character = self._advance()
            if character == quote:
                break
            if character != "\\":
                characters.append(character)
                continue

            if self._is_eof():
                raise PineSyntaxError("Unterminated escape sequence", location)
            escaped = self._advance()
            characters.append(_ESCAPES.get(escaped, escaped))

        self._append(TokenType.STRING, "".join(characters), location)

    def _read_symbol(self) -> None:
        location = self._location()
        for text, kind in _SYMBOLS.items():
            if self._source.startswith(text, self._index):
                for _ in text:
                    self._advance()
                if kind is TokenType.LEFT_PAREN:
                    self._delimiters.append((text, location))
                elif kind is TokenType.LEFT_BRACKET:
                    self._delimiters.append((text, location))
                elif kind is TokenType.LEFT_BRACE:
                    self._delimiters.append((text, location))
                elif kind in {
                    TokenType.RIGHT_PAREN,
                    TokenType.RIGHT_BRACKET,
                    TokenType.RIGHT_BRACE,
                }:
                    if not self._delimiters:
                        raise PineSyntaxError(f"Unexpected {text!r}", location)
                    opening, opening_location = self._delimiters.pop()
                    if _CLOSING_DELIMITERS[text] != opening:
                        raise PineSyntaxError(
                            f"Mismatched delimiter: {opening!r} opened at {opening_location}",
                            location,
                        )
                self._append(kind, text, location)
                return

        character = self._peek()
        raise PineSyntaxError(f"Unexpected character {character!r}", location)

    def _finish(self) -> None:
        if self._delimiters:
            opening, location = self._delimiters[-1]
            expected = _OPENING_DELIMITERS[opening]
            raise PineSyntaxError(
                f"Unclosed delimiter {opening!r}; expected {expected!r}",
                location,
            )

        if self._tokens and self._tokens[-1].kind not in {
            TokenType.NEWLINE,
            TokenType.DEDENT,
        }:
            self._append(TokenType.NEWLINE, "\\n", self._location())

        location = self._location()
        while len(self._indents) > 1:
            self._append(TokenType.DEDENT, "<dedent>", location)
            self._indents.pop()
        self._append(TokenType.EOF, "", location)


def tokenize(source: str, filename: str = "<string>") -> Sequence[Token]:
    """Convenience function that tokenizes source text."""

    return Lexer(source, filename).tokenize()
