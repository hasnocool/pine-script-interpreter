from pathlib import Path

import pytest

from pine_interpreter import Lexer, PineSyntaxError, TokenType

EXAMPLE = Path(__file__).parents[1] / "examples" / "01_basics.pine"


def test_lexer_reads_example_tokens() -> None:
    tokens = Lexer(EXAMPLE.read_text(encoding="utf-8"), str(EXAMPLE)).tokenize()
    kinds = [token.kind for token in tokens]

    first_identifier = kinds.index(TokenType.IDENTIFIER)
    assert kinds[first_identifier : first_identifier + 6] == [
        TokenType.IDENTIFIER,
        TokenType.EQUAL,
        TokenType.NUMBER,
        TokenType.PLUS,
        TokenType.NUMBER,
        TokenType.NEWLINE,
    ]
    assert TokenType.INDENT in kinds
    assert TokenType.DEDENT in kinds
    assert kinds[-1] is TokenType.EOF


def test_blank_and_comment_only_lines_do_not_create_indentation() -> None:
    tokens = Lexer("\n  // ignored\nvalue = 1\n").tokenize()
    kinds = [token.kind for token in tokens]

    assert TokenType.INDENT not in kinds
    assert TokenType.DEDENT not in kinds
    assert [kind for kind in kinds if kind is not TokenType.NEWLINE] == [
        TokenType.IDENTIFIER,
        TokenType.EQUAL,
        TokenType.NUMBER,
        TokenType.EOF,
    ]


def test_lexer_reports_unterminated_string() -> None:
    with pytest.raises(PineSyntaxError, match="Unterminated string"):
        Lexer('value = "broken\n').tokenize()


def test_lexer_reads_legacy_strings_colors_and_reassignment() -> None:
    tokens = Lexer("label = 'old'\ncolorValue = #A0B1C2\nvalue := 1\n").tokenize()
    kinds = [token.kind for token in tokens]

    assert kinds[:3] == [TokenType.IDENTIFIER, TokenType.EQUAL, TokenType.STRING]
    assert tokens[2].value == "old"
    assert TokenType.COLOR in kinds
    assert TokenType.COLON_EQUAL in kinds


def test_lexer_treats_wrapped_expression_indentation_as_continuation() -> None:
    tokens = Lexer("value = condition ? 1 :\n    2\n").tokenize()
    kinds = [token.kind for token in tokens]

    assert TokenType.INDENT not in kinds
    assert TokenType.DEDENT not in kinds
    assert kinds.count(TokenType.NEWLINE) == 1


def test_lexer_reports_unclosed_delimiter_with_opening_location() -> None:
    with pytest.raises(PineSyntaxError, match="Unclosed delimiter"):
        Lexer("value = f(1\n").tokenize()


def test_lexer_reads_triple_quoted_and_wrapped_strings() -> None:
    tokens = Lexer("message = '''first\nsecond'''\nvalue = 1\n").tokenize()
    kinds = [token.kind for token in tokens]

    assert tokens[2].value == "first\nsecond"
    assert kinds.count(TokenType.NEWLINE) == 2


def test_lexer_handles_operator_leading_wrapping_and_realigned_ternaries() -> None:
    tokens = Lexer("value = condition ? first :\n  second\n").tokenize()
    assert TokenType.INDENT not in [token.kind for token in tokens]
    assert TokenType.DEDENT not in [token.kind for token in tokens]
