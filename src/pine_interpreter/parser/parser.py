"""Recursive-descent parser for Pine Script source."""

from __future__ import annotations

from collections.abc import Sequence

from pine_interpreter.diagnostics import PineSyntaxError
from pine_interpreter.lexer import Token, TokenType
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
    EnumMember,
    Expression,
    ExpressionStatement,
    FieldDeclaration,
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
    NumberLiteral,
    Parameter,
    Program,
    Statement,
    StringLiteral,
    SwitchCase,
    SwitchExpression,
    SwitchStatement,
    TupleDeclaration,
    TypeDeclaration,
    TypeReference,
    UnaryExpression,
    VariableDeclaration,
    WhileStatement,
)

_BINARY_PRECEDENCE: dict[TokenType, int] = {
    TokenType.OR: 1,
    TokenType.AND: 2,
    TokenType.EQUAL_EQUAL: 3,
    TokenType.BANG_EQUAL: 3,
    TokenType.LESS: 3,
    TokenType.LESS_EQUAL: 3,
    TokenType.GREATER: 3,
    TokenType.GREATER_EQUAL: 3,
    TokenType.PLUS: 4,
    TokenType.MINUS: 4,
    TokenType.STAR: 5,
    TokenType.SLASH: 5,
    TokenType.PERCENT: 5,
    TokenType.DOUBLE_STAR: 7,
}

_ASSIGNMENT_OPERATORS = {
    TokenType.COLON_EQUAL: ":=",
    TokenType.PLUS_EQUAL: "+=",
    TokenType.MINUS_EQUAL: "-=",
    TokenType.STAR_EQUAL: "*=",
    TokenType.SLASH_EQUAL: "/=",
    TokenType.PERCENT_EQUAL: "%=",
}

_DECLARATION_QUALIFIERS = {"const", "input", "simple", "series"}
_STORAGE_MODIFIERS = {"var", "varip"}


class Parser:
    """Build an immutable AST from a Pine token stream."""

    def __init__(self, tokens: Sequence[Token]) -> None:
        self._tokens = tokens
        self._index = 0
        self._parsing_switch_pattern = False

    @property
    def _current(self) -> Token:
        return self._tokens[self._index]

    def _kind(self) -> TokenType:
        return self._current.kind

    def parse(self) -> Program:
        """Parse all top-level statements."""

        statements: list[Statement] = []
        location = self._current.location

        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() is TokenType.INDENT:
                self._raise_unexpected("a top-level statement")
            if self._kind() is TokenType.DEDENT:
                # Visual wrapping and legacy scripts can leave a dedent after
                # the parser has already closed the corresponding block.
                self._advance()
                continue
            if self._kind() is TokenType.COMMA:
                # A few legacy scripts leave declaration arguments comma-led
                # after a wrapped call. Treat them as ordinary assignments.
                self._advance()
                if self._at_end():
                    continue

            if self._kind() is TokenType.ELSE:
                self._advance()
                self._skip_separators()
                if self._kind() is TokenType.INDENT:
                    statements.extend(self._parse_block())
                elif self._starts_statement_token():
                    statements.extend(self._parse_visual_block(self._current.location.column))
                continue
            statements.append(self._parse_statement())
            while self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is TokenType.NEWLINE:
                    next_index = self._index
                    while self._kind_at(next_index) is TokenType.NEWLINE:
                        next_index += 1
                    if self._kind_at(next_index) is TokenType.INDENT:
                        while self._kind() is TokenType.NEWLINE:
                            self._advance()
                        statements.extend(self._parse_block())
                    break
                statements.append(self._parse_statement())
            if not self._at_statement_end():
                if self._starts_statement_token():
                    continue
                self._raise_unexpected("the end of a statement")

        return Program(location, tuple(statements))

    def _parse_statement(self) -> Statement:
        if self._kind() is TokenType.IF:
            return self._parse_if_statement()
        if self._kind() is TokenType.FOR:
            return self._parse_for_statement()
        if self._kind() is TokenType.WHILE:
            return self._parse_while_statement()
        if self._is_contextual_keyword("break") and self._at_statement_end():
            location = self._current.location
            self._advance()
            return BreakStatement(location)
        if self._is_contextual_keyword("continue") and self._at_statement_end():
            location = self._current.location
            self._advance()
            return ContinueStatement(location)
        if self._is_switch_ahead():
            location = self._current.location
            subject, cases = self._parse_switch_components()
            return SwitchStatement(location, subject, cases)

        if self._is_contextual_keyword("import"):
            return self._parse_import_statement()
        if self._is_type_declaration_ahead("type"):
            return self._parse_type_declaration()
        if self._is_type_declaration_ahead("enum"):
            return self._parse_enum_declaration()
        if self._is_function_declaration():
            return self._parse_function_declaration()
        if self._kind() is TokenType.LEFT_BRACKET:
            tuple_declaration = self._try_parse_tuple_declaration()
            if tuple_declaration is not None:
                return tuple_declaration

        declaration = self._try_parse_typed_declaration()
        if declaration is not None:
            return declaration

        location = self._current.location
        expression = self._parse_expression()
        if self._kind() is TokenType.EQUAL and isinstance(expression, Identifier):
            name = expression.name
            self._advance()
            return VariableDeclaration(location, name, self._parse_expression())
        if self._kind() in _ASSIGNMENT_OPERATORS:
            operator = _ASSIGNMENT_OPERATORS[self._kind()]
            self._advance()
            return AssignmentStatement(location, expression, operator, self._parse_expression())
        return ExpressionStatement(location, expression)

    def _parse_control_condition(self) -> Expression:
        if self._kind() is TokenType.NEWLINE:
            self._advance()
            while self._kind() is TokenType.NEWLINE:
                self._advance()
            if self._kind() is TokenType.INDENT:
                self._advance()
        return self._parse_expression()

    def _parse_if_statement(self) -> IfStatement:
        location = self._current.location
        self._advance()
        condition = self._parse_control_condition()
        self._expect_newline()
        then_branch = self._parse_body(location.column)
        else_branch = self._parse_optional_else()
        return IfStatement(location, condition, then_branch, else_branch)

    def _parse_if_expression(self) -> IfExpression:
        location = self._current.location
        self._advance()
        condition = self._parse_control_condition()
        self._expect_newline()
        then_branch = self._parse_body(location.column)
        else_branch = self._parse_optional_else()
        return IfExpression(location, condition, then_branch, else_branch)

    def _parse_optional_else(self) -> tuple[Statement, ...]:
        if self._kind() not in {TokenType.NEWLINE, TokenType.DEDENT, TokenType.ELSE}:
            return ()

        saved_index = self._index
        while self._kind() in {TokenType.NEWLINE, TokenType.DEDENT}:
            self._advance()
        if self._kind() is not TokenType.ELSE:
            self._index = saved_index
            return ()

        else_location = self._current.location
        self._advance()
        if self._kind() is TokenType.IF:
            return (self._parse_if_statement(),)
        self._expect_newline()
        return self._parse_body(else_location.column)

    def _parse_for_statement(self) -> ForStatement:
        location = self._current.location
        self._advance()
        variable: str | tuple[Expression, ...]
        if self._kind() is TokenType.LEFT_BRACKET:
            variable = self._parse_tuple_targets()
        elif (
            self._name_starts_at(self._index)
            and self._value_at(self._index + 1) != "in"
            and self._name_starts_at(self._index + 1)
        ):
            self._parse_type_reference()
            variable = self._expect_identifier().value
        else:
            if self._kind() is TokenType.WHILE:
                variable = self._advance().value
            else:
                variable = self._expect_identifier().value

        if self._kind() is TokenType.EQUAL:
            if not isinstance(variable, str):
                self._raise_unexpected("a counted loop variable")
            self._advance()
            start = self._parse_expression()
        elif self._is_contextual_keyword("in"):
            self._advance()
            start = self._parse_expression()
            self._expect_newline()
            return ForStatement(location, variable, start, None, None, self._parse_block())
        else:
            self._raise_unexpected("'=' or 'in' in a for statement")

        end: Expression | None = None
        step: Expression | None = None
        if self._is_contextual_keyword("to"):
            self._advance()
            end = self._parse_expression()
            if self._is_contextual_keyword("by"):
                self._advance()
                step = self._parse_expression()
        else:
            self._raise_unexpected("'to' in a counted for statement")

        self._expect_newline()
        return ForStatement(location, variable, start, end, step, self._parse_block())

    def _parse_while_statement(self) -> WhileStatement:
        location = self._current.location
        self._advance()
        condition = self._parse_expression()
        self._expect_newline()
        return WhileStatement(location, condition, self._parse_block())

    def _parse_switch_components(self) -> tuple[Expression | None, tuple[SwitchCase, ...]]:
        location = self._current.location
        self._advance()
        if self._kind() is TokenType.NEWLINE:
            subject = None
        elif self._current.location.line == location.line:
            subject = self._parse_expression()
        else:
            subject = None
        if subject is not None:
            if self._kind() is TokenType.NEWLINE:
                self._expect_newline()
            elif self._current.location.line <= location.line:
                self._expect_newline()
        elif self._kind() is TokenType.NEWLINE:
            self._skip_separators()
        cases = self._parse_block_switch_cases(self._kind() is TokenType.INDENT)
        if not cases:
            self._raise_at(self._current, "at least one switch case")
        _ = location
        return subject, cases

    def _parse_import_statement(self) -> ImportDeclaration:
        location = self._current.location
        self._advance()
        if self._kind() is TokenType.STRING:
            import_path = self._advance().value
        else:
            parts: list[str] = []
            while not self._at_statement_end() and not self._is_contextual_keyword("as"):
                parts.append(self._advance().value)
            import_path = "".join(parts)
            if not import_path:
                self._raise_unexpected("an import path")

        alias = None
        if self._is_contextual_keyword("as"):
            self._advance()
            alias = self._expect_identifier().value
        return ImportDeclaration(location, import_path, alias)

    def _parse_type_declaration(self) -> TypeDeclaration:
        location = self._current.location
        exported = self._is_contextual_keyword("export")
        if exported:
            self._advance()
        self._advance()
        name = self._expect_identifier().value
        self._expect_newline()
        members = self._parse_type_block()
        fields: list[FieldDeclaration] = []
        methods: list[FunctionDeclaration] = []
        for member in members:
            if isinstance(member, FunctionDeclaration):
                methods.append(member)
            elif isinstance(member, FieldDeclaration):
                fields.append(member)
        return TypeDeclaration(
            location,
            name,
            tuple(fields),
            tuple(methods),
            exported,
        )

    def _parse_field_declaration(self) -> FieldDeclaration:
        location = self._current.location
        if (
            self._kind() is TokenType.IDENTIFIER
            and self._current.value in _DECLARATION_QUALIFIERS | _STORAGE_MODIFIERS
        ):
            self._advance()
        if self._kind() is TokenType.IDENTIFIER and self._peek().kind is TokenType.EQUAL:
            name = self._advance().value
            self._advance()
            return FieldDeclaration(location, name, None, self._parse_expression())

        type_annotation = self._parse_type_reference()
        name = self._expect_identifier().value
        value = None
        if self._kind() is TokenType.EQUAL:
            self._advance()
            value = self._parse_expression()
        return FieldDeclaration(location, name, type_annotation, value)

    def _parse_enum_declaration(self) -> EnumDeclaration:
        location = self._current.location
        exported = self._is_contextual_keyword("export")
        if exported:
            self._advance()
        self._advance()
        name = self._expect_identifier().value
        self._expect_newline()
        self._expect(TokenType.INDENT)
        members: list[EnumMember] = []
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() is TokenType.DEDENT:
                self._advance()
                break
            entry_location = self._current.location
            if self._kind() is TokenType.NA:
                entry_name = self._advance().value
            else:
                entry_name = self._expect_identifier().value
            value = None
            if self._kind() is TokenType.EQUAL:
                self._advance()
                value = self._parse_expression()
            members.append(EnumMember(entry_location, entry_name, value))
            if not self._at_statement_end():
                self._raise_unexpected("the end of an enum member")
        return EnumDeclaration(location, name, tuple(members), exported)

    def _is_function_declaration(self) -> bool:
        if self._parsing_switch_pattern:
            return False
        index = self._index
        if self._kind_at(index) is TokenType.IDENTIFIER and self._value_at(index) == "export":
            index += 1
        if self._kind_at(index) is TokenType.IDENTIFIER and self._value_at(index) == "method":
            index += 1
            while self._kind_at(index) in {TokenType.NEWLINE, TokenType.INDENT}:
                index += 1
            if not self._name_starts_at(index):
                index = self._skip_type_at(index)
        if not self._name_starts_at(index):
            return False
        opening = index + 1
        if self._kind_at(opening) is not TokenType.LEFT_PAREN:
            return False
        closing = self._matching_delimiter(opening)
        if closing is None:
            return False
        arrow_index = closing + 1
        while self._kind_at(arrow_index) in {TokenType.NEWLINE, TokenType.INDENT}:
            arrow_index += 1
        if self._kind_at(arrow_index) is not TokenType.FAT_ARROW:
            return False
        name_token = self._tokens[index]
        arrow_token = self._tokens[arrow_index]
        has_declaration_modifier = index > 0 and self._value_at(index - 1) in {
            "export",
            "method",
        }
        if (
            arrow_token.location.line > name_token.location.line
            and arrow_token.location.column < name_token.location.column
            and not has_declaration_modifier
        ):
            # A call at the end of a switch case is followed by the next
            # case's arrow, which is visually dedented. Do not reinterpret
            # that call as a function declaration.
            return False
        return True

    def _extend_function_body(
        self, body: tuple[Statement, ...], header_column: int
    ) -> tuple[Statement, ...]:
        statements = list(body)
        while not self._at_end():
            saved_index = self._index
            while self._kind() in {
                TokenType.NEWLINE,
                TokenType.DEDENT,
                TokenType.SEMICOLON,
            }:
                self._advance()
            if self._current.location.column <= header_column or not self._starts_statement_token():
                self._index = saved_index
                break
            statements.append(self._parse_statement())
        return tuple(statements)

    def _parse_function_declaration(self) -> FunctionDeclaration:
        location = self._current.location
        exported = False
        if self._is_contextual_keyword("export"):
            exported = True
            self._advance()

        method = False
        return_type: TypeReference | None = None
        if self._is_contextual_keyword("method"):
            method = True
            self._advance()
            while self._kind() in {TokenType.NEWLINE, TokenType.INDENT}:
                self._advance()
            if not self._name_starts_at(self._index):
                return_type = self._parse_type_reference()

        if not self._name_starts_at(self._index):
            return_type = self._parse_type_reference()
        name = self._expect_identifier().value
        parameters = self._parse_parameters()
        if self._kind() is TokenType.NEWLINE:
            while self._kind() is TokenType.NEWLINE:
                self._advance()
            if self._kind() is TokenType.INDENT:
                self._advance()
        self._expect(TokenType.FAT_ARROW)

        if self._kind() is TokenType.NEWLINE:
            while self._kind() is TokenType.NEWLINE:
                self._advance()
            body = self._parse_block()
            body = self._extend_function_body(body, location.column)
        else:
            body = (self._parse_statement(),)
        return FunctionDeclaration(
            location,
            name,
            parameters,
            body,
            return_type,
            exported,
            method,
        )

    def _parse_parameters(self) -> tuple[Parameter, ...]:
        self._expect(TokenType.LEFT_PAREN)
        parameters: list[Parameter] = []
        if self._kind() is not TokenType.RIGHT_PAREN:
            while True:
                parameters.append(self._parse_parameter())
                if self._kind() is not TokenType.COMMA:
                    break
                self._advance()
                if self._kind() is TokenType.RIGHT_PAREN:
                    break
        self._expect(TokenType.RIGHT_PAREN)
        return tuple(parameters)

    def _parse_parameter(self) -> Parameter:
        location = self._current.location
        qualifier = None
        if (
            self._kind() is TokenType.IDENTIFIER
            and self._current.value in _DECLARATION_QUALIFIERS
            and self._peek().kind is TokenType.IDENTIFIER
        ):
            qualifier = self._advance().value

        type_annotation = None
        if self._name_starts_at(self._index) and self._peek().kind not in {
            TokenType.COMMA,
            TokenType.EQUAL,
            TokenType.RIGHT_PAREN,
        }:
            type_annotation = self._parse_type_reference()
        if self._kind() is TokenType.NA:
            name = self._advance().value
        else:
            name = self._expect_identifier().value
        default = None
        if self._kind() is TokenType.EQUAL:
            self._advance()
            default = self._parse_expression()
        return Parameter(location, name, type_annotation, default, qualifier)

    def _parse_type_reference(self) -> TypeReference:
        token = self._expect(TokenType.IDENTIFIER)
        name = token.value
        if self._kind() is TokenType.DOT:
            parts = [name]
            while self._kind() is TokenType.DOT:
                self._advance()
                parts.append(self._expect(TokenType.IDENTIFIER).value)
            name = ".".join(parts)

        arguments: list[TypeReference] = []
        if self._kind() is TokenType.LESS:
            self._advance()
            while self._kind() is not TokenType.GREATER:
                arguments.append(self._parse_type_reference())
                if self._kind() is TokenType.COMMA:
                    self._advance()
            self._expect(TokenType.GREATER)
        while self._kind() is TokenType.LEFT_BRACKET:
            self._advance()
            self._expect(TokenType.RIGHT_BRACKET)
            name += "[]"
        return TypeReference(token.location, name, tuple(arguments))

    def _try_parse_typed_declaration(self) -> VariableDeclaration | None:
        saved_index = self._index
        location = self._current.location
        storage = None
        qualifier = None
        exported = self._is_contextual_keyword("export")
        if exported:
            self._advance()

        if self._kind() is TokenType.IDENTIFIER and self._current.value in _STORAGE_MODIFIERS:
            storage = self._advance().value
        if self._kind() is TokenType.IDENTIFIER and self._current.value in _DECLARATION_QUALIFIERS:
            qualifier = self._advance().value

        if self._name_starts_at(self._index) and self._peek().kind is TokenType.EQUAL:
            name = self._advance().value
            self._advance()
            return VariableDeclaration(
                location,
                name,
                self._parse_expression(),
                None,
                storage,
                qualifier,
                exported,
            )

        if not self._name_starts_at(self._index):
            self._index = saved_index
            return None

        type_start = self._index
        try:
            type_annotation = self._parse_type_reference()
        except PineSyntaxError:
            self._index = saved_index
            return None

        if self._kind() is TokenType.NEWLINE:
            wrapped_index = self._index
            while self._kind_at(wrapped_index) is TokenType.NEWLINE:
                wrapped_index += 1
            if self._kind_at(wrapped_index) is TokenType.INDENT:
                wrapped_index += 1
            if (
                self._name_starts_at(wrapped_index)
                and self._kind_at(wrapped_index + 1) is TokenType.EQUAL
            ):
                self._index = wrapped_index
                name = self._advance().value
                self._advance()
                return VariableDeclaration(
                    location,
                    name,
                    self._parse_expression(),
                    type_annotation,
                    storage,
                    qualifier,
                    exported,
                )

        if self._name_starts_at(self._index):
            name = self._advance().value
            if self._kind() is TokenType.EQUAL:
                self._advance()
                value = self._parse_expression()
                return VariableDeclaration(
                    location,
                    name,
                    value,
                    type_annotation,
                    storage,
                    qualifier,
                    exported,
                )

        _ = type_start
        self._index = saved_index
        return None

    def _parse_tuple_targets(self) -> tuple[Expression, ...]:
        opening = self._advance()
        targets: list[Expression] = []
        if self._kind() is not TokenType.RIGHT_BRACKET:
            while True:
                targets.append(self._parse_expression())
                if self._kind() is not TokenType.COMMA:
                    break
                self._advance()
                if self._kind() is TokenType.RIGHT_BRACKET:
                    break
        self._expect(TokenType.RIGHT_BRACKET)
        _ = opening
        return tuple(targets)

    def _try_parse_tuple_declaration(self) -> TupleDeclaration | None:
        saved_index = self._index
        opening = self._advance()
        targets: list[Expression] = []
        if self._kind() is not TokenType.RIGHT_BRACKET:
            while True:
                targets.append(self._parse_expression())
                if self._kind() is not TokenType.COMMA:
                    break
                self._advance()
                if self._kind() is TokenType.RIGHT_BRACKET:
                    break
        if self._kind() is not TokenType.RIGHT_BRACKET:
            self._index = saved_index
            return None
        self._advance()
        if self._kind() is not TokenType.EQUAL:
            self._index = saved_index
            return None
        self._advance()
        return TupleDeclaration(opening.location, tuple(targets), self._parse_expression())

    def _parse_block(self) -> tuple[Statement, ...]:
        self._expect(TokenType.INDENT)
        statements: list[Statement] = []
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() is TokenType.ELSE:
                return tuple(statements)
            if self._kind() is TokenType.FAT_ARROW:
                return tuple(statements)
            if self._kind() is TokenType.DEDENT:
                self._advance()
                return tuple(statements)
            statements.append(self._parse_statement())
            while self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is TokenType.NEWLINE:
                    next_index = self._index
                    while self._kind_at(next_index) is TokenType.NEWLINE:
                        next_index += 1
                    if self._kind_at(next_index) is TokenType.INDENT:
                        while self._kind() is TokenType.NEWLINE:
                            self._advance()
                        statements.extend(self._parse_block())
                    break
                statements.append(self._parse_statement())
            if self._kind() in {
                TokenType.IF,
                TokenType.FOR,
                TokenType.WHILE,
                TokenType.SWITCH,
            }:
                return tuple(statements)
            if not self._at_statement_end():
                if self._kind() is TokenType.FAT_ARROW:
                    return tuple(statements)
                if self._starts_statement_token():
                    continue
                self._raise_unexpected("the end of a statement")
        return tuple(statements)

    def _parse_visual_block(self, header_column: int) -> tuple[Statement, ...]:
        while self._kind() is TokenType.DEDENT:
            next_index = self._index + 1
            while self._kind_at(next_index) is TokenType.NEWLINE:
                next_index += 1
            if (
                self._kind_at(next_index)
                in {
                    TokenType.EOF,
                    TokenType.ELSE,
                    TokenType.FAT_ARROW,
                }
                or self._location_column(next_index) <= header_column - 4
            ):
                break
            self._advance()

        statements: list[Statement] = []
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() in {
                TokenType.DEDENT,
                TokenType.ELSE,
                TokenType.FAT_ARROW,
            }:
                break
            if self._kind() is TokenType.INDENT:
                statements.extend(self._parse_block())
                break
            statements.append(self._parse_statement())
            if self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is TokenType.NEWLINE:
                    self._skip_separators()
                if not self._at_statement_end():
                    statements.append(self._parse_statement())
                continue
            if self._kind() in {
                TokenType.IF,
                TokenType.FOR,
                TokenType.WHILE,
                TokenType.SWITCH,
            }:
                break
            if not self._at_statement_end():
                self._raise_unexpected("the end of a statement")
        return tuple(statements)

    def _parse_body(self, header_column: int) -> tuple[Statement, ...]:
        if self._kind() is TokenType.INDENT:
            return self._parse_block()
        return self._parse_visual_block(header_column)

    def _parse_type_block(self) -> tuple[FieldDeclaration | FunctionDeclaration, ...]:
        self._expect(TokenType.INDENT)
        members: list[FieldDeclaration | FunctionDeclaration] = []
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() is TokenType.DEDENT:
                self._advance()
                return tuple(members)
            if self._is_function_declaration():
                members.append(self._parse_function_declaration())
            elif self._type_field_ahead():
                members.append(self._parse_field_declaration())
            else:
                self._raise_unexpected("a type field or method")
            if not self._at_statement_end():
                self._raise_unexpected("the end of a type member")
        return tuple(members)

    def _type_field_ahead(self) -> bool:
        index = self._index
        if not self._name_starts_at(index):
            return False
        if self._kind_at(index + 1) is TokenType.EQUAL:
            return True
        if self._value_at(index) in _DECLARATION_QUALIFIERS | _STORAGE_MODIFIERS:
            index += 1
        if not self._name_starts_at(index):
            return False
        index += 1
        if self._kind_at(index) is TokenType.DOT:
            index += 2
        if self._kind_at(index) is TokenType.LESS:
            depth = 0
            while index < len(self._tokens):
                kind = self._kind_at(index)
                if kind is TokenType.LESS:
                    depth += 1
                elif kind is TokenType.GREATER:
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
        while (
            self._kind_at(index) is TokenType.LEFT_BRACKET
            and self._kind_at(index + 1) is TokenType.RIGHT_BRACKET
        ):
            index += 2
        return self._name_starts_at(index) and self._kind_at(index + 1) in {
            TokenType.EQUAL,
            TokenType.NEWLINE,
            TokenType.SEMICOLON,
        }

    def _parse_expression(self, minimum_precedence: int = 0) -> Expression:
        expression = self._parse_prefix()

        while True:
            precedence = _BINARY_PRECEDENCE.get(self._kind(), 0)
            if precedence == 0 or precedence < minimum_precedence:
                break
            operator = self._advance()
            next_minimum = precedence if operator.kind is TokenType.DOUBLE_STAR else precedence + 1
            right = self._parse_expression(next_minimum)
            expression = BinaryExpression(
                operator.location,
                expression,
                operator.value,
                right,
            )

        if minimum_precedence == 0 and self._kind() is TokenType.QUESTION:
            self._advance()
            when_true = self._parse_expression()
            self._expect(TokenType.COLON)
            when_false = self._parse_expression()
            return ConditionalExpression(
                expression.location,
                expression,
                when_true,
                when_false,
            )
        return expression

    def _parse_prefix(self) -> Expression:
        token = self._current
        if self._is_switch_ahead():
            location = self._current.location
            subject, cases = self._parse_switch_components()
            return SwitchExpression(location, subject, cases)
        match token.kind:
            case TokenType.NUMBER:
                self._advance()
                value: int | float
                if token.value.lower().startswith("0x"):
                    value = int(token.value, 16)
                elif any(character in token.value for character in ".eE"):
                    value = float(token.value)
                else:
                    value = int(token.value)
                return self._parse_postfix(NumberLiteral(token.location, value))
            case TokenType.STRING:
                self._advance()
                return self._parse_postfix(StringLiteral(token.location, token.value))
            case TokenType.COLOR:
                self._advance()
                return self._parse_postfix(ColorLiteral(token.location, token.value))
            case TokenType.TRUE:
                self._advance()
                return self._parse_postfix(BooleanLiteral(token.location, True))
            case TokenType.FALSE:
                self._advance()
                return self._parse_postfix(BooleanLiteral(token.location, False))
            case TokenType.NA:
                self._advance()
                return self._parse_postfix(NaLiteral(token.location))
            case TokenType.IDENTIFIER:
                self._advance()
                expression: Expression = Identifier(token.location, token.value)
                expression = self._parse_postfix(expression)
                return expression
            case TokenType.PLUS | TokenType.MINUS | TokenType.NOT:
                self._advance()
                return UnaryExpression(token.location, token.value, self._parse_prefix())
            case TokenType.LEFT_PAREN:
                self._advance()
                expression = self._parse_expression()
                self._expect(TokenType.RIGHT_PAREN)
                return self._parse_postfix(expression)
            case TokenType.LEFT_BRACKET:
                return self._parse_array_literal()
            case TokenType.LEFT_BRACE:
                return self._parse_brace_literal()
            case TokenType.IF:
                return self._parse_if_expression()
            case TokenType.FOR:
                statement = self._parse_for_statement()
                if not isinstance(statement, ForStatement):
                    raise AssertionError("for parsing returned an unexpected node")
                return ForExpression(
                    statement.location,
                    statement.variable,
                    statement.start,
                    statement.end,
                    statement.step,
                    statement.body,
                )
            case _:
                self._raise_unexpected("an expression")
                raise AssertionError("unreachable")

    def _parse_postfix(self, expression: Expression) -> Expression:
        while True:
            if self._kind() is TokenType.DOT:
                self._advance()
                property_location = self._current.location
                if self._kind() is TokenType.NA:
                    property_name = self._advance().value
                else:
                    property_name = self._expect_identifier().value
                expression = MemberExpression(
                    property_location,
                    expression,
                    property_name,
                )
                continue
            if self._kind() is TokenType.LEFT_PAREN:
                if self._is_visual_case_pattern(expression):
                    return expression
                # Legacy Pine permits comma-separated assignments followed by
                # a parenthesized final expression on the next physical line.
                # The lexer intentionally omits that newline when a call could
                # otherwise continue, so use source columns to keep the ``(``
                # from becoming a dynamic call target on the preceding call.
                if (
                    self._current.location.line > expression.location.line
                    and self._current.location.column <= expression.location.column
                ):
                    return expression
                expression = self._parse_call(expression)
                continue
            if self._kind() is TokenType.LESS and self._generic_call_ahead():
                self._advance()
                type_arguments: list[TypeReference] = []
                while self._kind() is not TokenType.GREATER:
                    type_arguments.append(self._parse_type_reference())
                    if self._kind() is TokenType.COMMA:
                        self._advance()
                self._expect(TokenType.GREATER)
                expression = self._parse_call(expression, tuple(type_arguments))
                continue
            if self._kind() is TokenType.LEFT_BRACKET:
                location = self._current.location
                self._advance()
                offset = self._parse_expression()
                self._expect(TokenType.RIGHT_BRACKET)
                expression = HistoryExpression(location, expression, offset)
                continue
            return expression

    def _parse_call(
        self,
        callee: Expression,
        type_arguments: tuple[TypeReference, ...] = (),
    ) -> CallExpression:
        location = self._current.location
        self._expect(TokenType.LEFT_PAREN)
        arguments: list[Expression | CallArgument] = []

        if self._kind() is not TokenType.RIGHT_PAREN:
            while True:
                if self._kind() is TokenType.IDENTIFIER and self._peek().kind is TokenType.EQUAL:
                    name = self._advance().value
                    self._advance()
                    arguments.append(CallArgument(callee.location, name, self._parse_expression()))
                else:
                    arguments.append(self._parse_expression())
                if self._kind() is not TokenType.COMMA:
                    break
                self._advance()
                if self._kind() is TokenType.RIGHT_PAREN:
                    break
        self._expect(TokenType.RIGHT_PAREN)
        return CallExpression(location, callee, tuple(arguments), type_arguments)

    def _parse_array_literal(self) -> ArrayLiteral:
        location = self._current.location
        self._expect(TokenType.LEFT_BRACKET)
        elements: list[Expression] = []
        if self._kind() is not TokenType.RIGHT_BRACKET:
            while True:
                elements.append(self._parse_expression())
                if self._kind() is not TokenType.COMMA:
                    break
                self._advance()
                if self._kind() is TokenType.RIGHT_BRACKET:
                    break
        self._expect(TokenType.RIGHT_BRACKET)
        return ArrayLiteral(location, tuple(elements))

    def _parse_brace_literal(self) -> BraceLiteral:
        location = self._current.location
        self._expect(TokenType.LEFT_BRACE)
        elements: list[Expression] = []
        if self._kind() is not TokenType.RIGHT_BRACE:
            while True:
                elements.append(self._parse_expression())
                if self._kind() is TokenType.COMMA:
                    self._advance()
                    continue
                break
        self._expect(TokenType.RIGHT_BRACE)
        return BraceLiteral(location, tuple(elements))

    def _current_statement_has_assignment(self) -> bool:
        depth = 0
        for offset in range(self._index, min(len(self._tokens), self._index + 80)):
            kind = self._kind_at(offset)
            if kind in {TokenType.NEWLINE, TokenType.SEMICOLON} and depth == 0:
                break
            if kind in {
                TokenType.LEFT_PAREN,
                TokenType.LEFT_BRACKET,
                TokenType.LEFT_BRACE,
            }:
                depth += 1
            elif kind in {
                TokenType.RIGHT_PAREN,
                TokenType.RIGHT_BRACKET,
                TokenType.RIGHT_BRACE,
            }:
                depth = max(0, depth - 1)
            elif depth == 0 and kind in {
                TokenType.EQUAL,
                TokenType.COLON_EQUAL,
                TokenType.PLUS_EQUAL,
                TokenType.MINUS_EQUAL,
                TokenType.STAR_EQUAL,
                TokenType.SLASH_EQUAL,
                TokenType.PERCENT_EQUAL,
            }:
                return True
        return False

    def _switch_body_boundary(self, case_column: int) -> bool:
        if not case_column or self._current.location.column > case_column:
            return False
        if self._value_at(self._index) in {
            "var",
            "varip",
            "const",
            "export",
            "method",
        }:
            return True
        return not self._current_statement_has_assignment()

    def _parse_switch_case_block(self, case_location: object) -> tuple[Statement, ...]:
        self._expect(TokenType.INDENT)
        statements: list[Statement] = []
        case_column = getattr(case_location, "column", 0)
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if self._kind() in {TokenType.FAT_ARROW, TokenType.ELSE}:
                return tuple(statements)
            if self._switch_body_boundary(case_column) and self._kind() not in {
                TokenType.NEWLINE,
                TokenType.DEDENT,
                TokenType.SEMICOLON,
                TokenType.FAT_ARROW,
                TokenType.ELSE,
            }:
                return tuple(statements)
            if self._kind() is TokenType.DEDENT:
                next_index = self._index + 1
                while self._kind_at(next_index) in {
                    TokenType.NEWLINE,
                    TokenType.DEDENT,
                }:
                    next_index += 1
                if (
                    self._kind_at(next_index)
                    not in {
                        TokenType.EOF,
                        TokenType.ELSE,
                        TokenType.FAT_ARROW,
                    }
                    and self._location_column(next_index) <= 1
                ):
                    # A top-level statement ends every enclosing block. Leave
                    # the dedents in place so each outer block terminates in
                    # turn instead of swallowing the statement.
                    return tuple(statements)
                if self._location_column(next_index) >= case_column and self._switch_marker_ahead(
                    next_index, allow_call=True
                ):
                    self._index = next_index
                elif self._kind_at(next_index) not in {
                    TokenType.EOF,
                    TokenType.ELSE,
                    TokenType.FAT_ARROW,
                }:
                    self._index = next_index
                else:
                    self._advance()
                return tuple(statements)
            if self._kind() is TokenType.INDENT:
                statements.extend(self._parse_block())
                continue
            statements.append(self._parse_statement())
            while self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is TokenType.NEWLINE:
                    self._skip_separators()
                if self._kind() is TokenType.INDENT:
                    self._advance()
                if self._kind() in {
                    TokenType.EOF,
                    TokenType.DEDENT,
                    TokenType.FAT_ARROW,
                }:
                    break
                statements.append(self._parse_statement())
            if self._kind() in {TokenType.FAT_ARROW, TokenType.ELSE}:
                return tuple(statements)
            if self._switch_body_boundary(case_column) and self._kind() not in {
                TokenType.NEWLINE,
                TokenType.DEDENT,
                TokenType.SEMICOLON,
                TokenType.FAT_ARROW,
                TokenType.ELSE,
            }:
                return tuple(statements)
            if not self._at_statement_end():
                if self._starts_statement_token():
                    continue
                self._raise_unexpected("the end of a switch case body")
        return tuple(statements)

    def _parse_switch_visual_body(self, case_location: object) -> tuple[Statement, ...]:
        if self._kind() is TokenType.INDENT:
            return self._parse_switch_case_block(case_location)
        if self._kind() in {
            TokenType.EOF,
            TokenType.DEDENT,
            TokenType.FAT_ARROW,
            TokenType.ELSE,
        }:
            return ()

        statements: list[Statement] = []
        case_column = getattr(case_location, "column", 0)
        while not self._at_end():
            if self._kind() in {
                TokenType.EOF,
                TokenType.DEDENT,
                TokenType.FAT_ARROW,
                TokenType.ELSE,
            }:
                break
            statements.append(self._parse_statement())
            continued = False
            while self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is not TokenType.NEWLINE:
                    if self._kind() in {
                        TokenType.EOF,
                        TokenType.DEDENT,
                        TokenType.FAT_ARROW,
                    }:
                        break
                    statements.append(self._parse_statement())
                    continue
                newline_index = self._index
                next_index = self._index
                while self._kind_at(next_index) in {
                    TokenType.NEWLINE,
                    TokenType.DEDENT,
                }:
                    next_index += 1
                if self._kind_at(next_index) is TokenType.INDENT:
                    while self._kind() is TokenType.NEWLINE:
                        self._advance()
                    statements.extend(self._parse_block())
                    return tuple(statements)
                if (
                    self._kind_at(next_index) is not TokenType.FAT_ARROW
                    and self._kind_at(next_index) is not TokenType.EOF
                    and self._location_column(next_index) > case_column
                ):
                    self._index = next_index
                    continued = True
                    break
                self._index = newline_index
                break
            if continued:
                continue
            if self._kind() is not TokenType.NEWLINE:
                break
            newline_index = self._index
            next_index = self._index
            while self._kind_at(next_index) in {
                TokenType.NEWLINE,
                TokenType.DEDENT,
            }:
                next_index += 1
            if (
                self._kind_at(next_index) is not TokenType.FAT_ARROW
                and self._kind_at(next_index) is not TokenType.EOF
                and self._location_column(next_index) > case_column
            ):
                self._index = next_index
                continue
            self._index = newline_index
            break
        return tuple(statements)

    def _is_visual_case_pattern(self, expression: Expression) -> bool:
        if self._kind() is not TokenType.LEFT_PAREN:
            return False
        if self._current.location.line <= expression.location.line:
            return False
        closing = self._matching_delimiter(self._index)
        if closing is None:
            return False
        arrow_index = closing + 1
        while self._kind_at(arrow_index) in {
            TokenType.NEWLINE,
            TokenType.INDENT,
        }:
            arrow_index += 1
        return self._kind_at(arrow_index) is TokenType.FAT_ARROW

    def _next_switch_case_ahead(self, case_line: int) -> bool:
        return (
            self._current.location.line > case_line
            and self._kind() not in {TokenType.FAT_ARROW, TokenType.COMMA}
            and self._switch_marker_ahead(self._index, allow_call=True)
        )

    def _parse_switch_case(self) -> SwitchCase:
        location = self._current.location
        self._parsing_switch_pattern = True
        try:
            if self._kind() is TokenType.FAT_ARROW:
                patterns: tuple[Expression, ...] = ()
            else:
                parsed_patterns: list[Expression] = []
                while True:
                    parsed_patterns.append(self._parse_expression())
                    if self._kind() is not TokenType.COMMA:
                        break
                    self._advance()
                patterns = tuple(parsed_patterns)
        finally:
            self._parsing_switch_pattern = False

        if self._kind() is TokenType.NEWLINE:
            self._expect_newline()
            if self._kind() is TokenType.INDENT:
                self._advance()
        self._expect(TokenType.FAT_ARROW)
        if self._kind() is TokenType.NEWLINE:
            self._expect_newline()
            body = self._parse_switch_visual_body(location)
        else:
            parsed_body: list[Statement] = [self._parse_statement()]
            if self._next_switch_case_ahead(location.line):
                return SwitchCase(location, patterns, tuple(parsed_body))
            while self._kind() is TokenType.COMMA:
                self._advance()
                if self._kind() is TokenType.NEWLINE:
                    newline_index = self._index
                    next_index = self._index
                    while self._kind_at(next_index) in {
                        TokenType.NEWLINE,
                        TokenType.DEDENT,
                    }:
                        next_index += 1
                    if self._kind_at(next_index) is TokenType.INDENT:
                        while self._kind() is TokenType.NEWLINE:
                            self._advance()
                        parsed_body.extend(self._parse_block())
                        break
                    if (
                        self._kind_at(next_index) is not TokenType.FAT_ARROW
                        and self._kind_at(next_index) is not TokenType.EOF
                        and self._location_column(next_index) > location.column
                    ):
                        self._index = next_index
                        parsed_body.append(self._parse_statement())
                        continue
                    self._index = newline_index
                    break
                parsed_body.append(self._parse_statement())
                if self._next_switch_case_ahead(location.line):
                    return SwitchCase(location, patterns, tuple(parsed_body))
            body = tuple(parsed_body)
        return SwitchCase(location, patterns, body)

    def _starts_statement_token(self) -> bool:
        return self._kind() in {
            TokenType.IDENTIFIER,
            TokenType.NUMBER,
            TokenType.STRING,
            TokenType.COLOR,
            TokenType.TRUE,
            TokenType.FALSE,
            TokenType.NA,
            TokenType.LEFT_BRACKET,
            TokenType.LEFT_BRACE,
            TokenType.LEFT_PAREN,
            TokenType.NOT,
            TokenType.PLUS,
            TokenType.MINUS,
            TokenType.IF,
            TokenType.FOR,
            TokenType.WHILE,
            TokenType.SWITCH,
        }

    def _at_end(self) -> bool:
        return self._kind() is TokenType.EOF

    def _at_statement_end(self) -> bool:
        return self._at_end() or self._kind() in {
            TokenType.NEWLINE,
            TokenType.SEMICOLON,
            TokenType.DEDENT,
            TokenType.ELSE,
            TokenType.IF,
            TokenType.FOR,
            TokenType.WHILE,
            TokenType.SWITCH,
        }

    def _skip_separators(self) -> None:
        while self._kind() in {TokenType.NEWLINE, TokenType.SEMICOLON}:
            self._advance()

    def _switch_marker_ahead(self, index: int, allow_call: bool = False) -> bool:
        if self._kind_at(index) is TokenType.IDENTIFIER and self._value_at(index) in {
            "method",
            "export",
            "switch",
            "if",
            "for",
            "while",
        }:
            return False
        if self._name_starts_at(index) and self._kind_at(index + 1) is TokenType.LEFT_PAREN:
            closing = self._matching_delimiter(index + 1)
            if closing is not None:
                if any(
                    self._kind_at(parameter_index) is TokenType.IDENTIFIER
                    and self._value_at(parameter_index)
                    in {
                        "bool",
                        "color",
                        "float",
                        "int",
                        "line",
                        "matrix",
                        "series",
                        "simple",
                        "string",
                        "table",
                    }
                    for parameter_index in range(index + 2, closing)
                ):
                    return False
                arrow_index = closing + 1
                while self._kind_at(arrow_index) in {
                    TokenType.NEWLINE,
                    TokenType.INDENT,
                }:
                    arrow_index += 1
                if self._kind_at(arrow_index) is TokenType.FAT_ARROW:
                    if not allow_call:
                        return False
                    after_arrow = arrow_index + 1
                    saw_indent = False
                    while self._kind_at(after_arrow) in {
                        TokenType.NEWLINE,
                        TokenType.INDENT,
                    }:
                        saw_indent = saw_indent or self._kind_at(after_arrow) is TokenType.INDENT
                        after_arrow += 1
                    if saw_indent:
                        return False
        for offset in range(index, min(len(self._tokens), index + 80)):
            kind = self._kind_at(offset)
            if kind is TokenType.FAT_ARROW:
                return True
            if kind is TokenType.NEWLINE:
                next_index = offset + 1
                while self._kind_at(next_index) is TokenType.INDENT:
                    next_index += 1
                if self._kind_at(next_index) is not TokenType.FAT_ARROW:
                    return False
            if kind in {
                TokenType.EQUAL,
                TokenType.COLON_EQUAL,
                TokenType.PLUS_EQUAL,
                TokenType.MINUS_EQUAL,
                TokenType.STAR_EQUAL,
                TokenType.SLASH_EQUAL,
                TokenType.PERCENT_EQUAL,
            }:
                return False
            if kind in {
                TokenType.EOF,
                TokenType.SEMICOLON,
                TokenType.INDENT,
                TokenType.DEDENT,
            }:
                return False
        return False

    def _parse_block_switch_cases(self, require_indent: bool = True) -> tuple[SwitchCase, ...]:
        if require_indent:
            self._expect(TokenType.INDENT)
        cases: list[SwitchCase] = []
        case_column: int | None = None
        while not self._at_end():
            self._skip_separators()
            if self._at_end():
                break
            if (
                case_column is not None
                and self._kind() is TokenType.INDENT
                and self._current.location.column <= case_column
            ):
                self._advance()
                continue
            if (
                case_column is not None
                and self._current.location.column <= case_column
                and self._kind() is not TokenType.FAT_ARROW
                and self._kind() is not TokenType.DEDENT
                and not self._switch_marker_ahead(self._index, allow_call=True)
            ):
                return tuple(cases)
            if self._kind() is TokenType.DEDENT:
                next_index = self._index + 1
                while self._kind_at(next_index) in {
                    TokenType.NEWLINE,
                    TokenType.DEDENT,
                }:
                    next_index += 1
                if (
                    self._kind_at(next_index)
                    not in {
                        TokenType.EOF,
                        TokenType.ELSE,
                        TokenType.FAT_ARROW,
                    }
                    and self._location_column(next_index) <= 1
                ):
                    # A top-level statement ends every enclosing block. Leave
                    # the dedents in place so each outer block terminates in
                    # turn instead of swallowing the statement.
                    return tuple(cases)
                if (
                    case_column is not None
                    and self._location_column(next_index) <= case_column
                    and self._switch_marker_ahead(next_index, allow_call=True)
                ):
                    self._index = next_index
                    continue
                self._index = next_index
                return tuple(cases)
            if case_column is None:
                case_column = self._current.location.column
            if (
                cases
                and self._current.location.column > case_column
                and self._current_statement_has_assignment()
            ):
                return tuple(cases)
            if (
                cases
                and self._current.location.column > case_column
                and (
                    self._kind()
                    in {
                        TokenType.SWITCH,
                        TokenType.IF,
                        TokenType.FOR,
                        TokenType.WHILE,
                    }
                    or self._is_contextual_keyword("switch")
                )
            ):
                statement = self._parse_statement()
                previous = cases[-1]
                cases[-1] = SwitchCase(
                    previous.location,
                    previous.patterns,
                    (*previous.body, statement),
                )
                continue
            if self._kind() is TokenType.FAT_ARROW:
                cases.append(self._parse_switch_case())
                continue
            cases.append(self._parse_switch_case())
            if (
                not self._at_statement_end()
                and self._kind() is not TokenType.FAT_ARROW
                and not (case_column is not None and self._current.location.column <= case_column)
            ):
                self._raise_unexpected("the end of a switch case")
        return tuple(cases)

    def _is_contextual_keyword(self, value: str) -> bool:
        return self._kind() is TokenType.IDENTIFIER and self._current.value == value

    def _is_type_declaration_ahead(self, declaration_kind: str) -> bool:
        index = self._index
        if self._kind_at(index) is TokenType.IDENTIFIER and self._value_at(index) == "export":
            index += 1
        return (
            self._kind_at(index) is TokenType.IDENTIFIER
            and self._value_at(index) == declaration_kind
            and self._name_starts_at(index + 1)
            and self._kind_at(index + 2) is TokenType.NEWLINE
        )

    def _is_switch_ahead(self) -> bool:
        if not self._is_contextual_keyword("switch"):
            return False
        if self._kind_at(self._index + 1) in {
            TokenType.EQUAL,
            TokenType.COLON_EQUAL,
            TokenType.PLUS_EQUAL,
            TokenType.MINUS_EQUAL,
            TokenType.STAR_EQUAL,
            TokenType.SLASH_EQUAL,
            TokenType.PERCENT_EQUAL,
            TokenType.EQUAL_EQUAL,
            TokenType.BANG_EQUAL,
            TokenType.LESS,
            TokenType.LESS_EQUAL,
            TokenType.GREATER,
            TokenType.GREATER_EQUAL,
            TokenType.AND,
            TokenType.OR,
            TokenType.PLUS,
            TokenType.MINUS,
            TokenType.STAR,
            TokenType.SLASH,
            TokenType.PERCENT,
            TokenType.COMMA,
            TokenType.RIGHT_PAREN,
            TokenType.RIGHT_BRACKET,
            TokenType.COLON,
            TokenType.QUESTION,
        }:
            return False
        index = self._index + 1
        while index < len(self._tokens):
            kind = self._tokens[index].kind
            if kind is TokenType.FAT_ARROW:
                return True
            if kind is TokenType.NEWLINE:
                while self._kind_at(index) is TokenType.NEWLINE:
                    index += 1
                return self._kind_at(index) is TokenType.INDENT
            if kind in {TokenType.EOF, TokenType.SEMICOLON}:
                return False
            index += 1
        return False

    def _name_starts_at(self, index: int) -> bool:
        return self._kind_at(index) is TokenType.IDENTIFIER

    def _kind_at(self, index: int) -> TokenType:
        if index < 0 or index >= len(self._tokens):
            return TokenType.EOF
        return self._tokens[index].kind

    def _location_column(self, index: int) -> int:
        if index < 0 or index >= len(self._tokens):
            return 0
        return self._tokens[index].location.column

    def _value_at(self, index: int) -> str:
        if index < 0 or index >= len(self._tokens):
            return ""
        return self._tokens[index].value

    def _peek(self, offset: int = 1) -> Token:
        index = min(self._index + offset, len(self._tokens) - 1)
        return self._tokens[index]

    def _generic_call_ahead(self) -> bool:
        depth = 0
        for index in range(self._index, len(self._tokens)):
            kind = self._tokens[index].kind
            if kind is TokenType.LESS:
                depth += 1
            elif kind is TokenType.GREATER:
                depth -= 1
                if depth == 0:
                    return self._kind_at(index + 1) is TokenType.LEFT_PAREN
            elif kind not in {
                TokenType.IDENTIFIER,
                TokenType.DOT,
                TokenType.COMMA,
            }:
                return False
        return False

    def _matching_delimiter(self, opening_index: int) -> int | None:
        if self._kind_at(opening_index) is not TokenType.LEFT_PAREN:
            return None
        depth = 0
        for index in range(opening_index, len(self._tokens)):
            kind = self._tokens[index].kind
            if kind is TokenType.LEFT_PAREN:
                depth += 1
            elif kind is TokenType.RIGHT_PAREN:
                depth -= 1
                if depth == 0:
                    return index
            elif kind is TokenType.EOF:
                return None
        return None

    def _skip_type_at(self, index: int) -> int:
        if not self._name_starts_at(index):
            return index
        index += 1
        if self._kind_at(index) is TokenType.DOT:
            index += 2
        if self._kind_at(index) is not TokenType.LESS:
            return index
        depth = 0
        while index < len(self._tokens):
            kind = self._kind_at(index)
            if kind is TokenType.LESS:
                depth += 1
            elif kind is TokenType.GREATER:
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return index

    def _expect_newline(self) -> None:
        if self._kind() not in {TokenType.NEWLINE, TokenType.SEMICOLON}:
            self._raise_unexpected("the end of a statement")
        self._advance()
        while self._kind() is TokenType.NEWLINE:
            self._advance()

    def _expect(self, kind: TokenType) -> Token:
        if self._kind() is not kind:
            self._raise_unexpected(kind.name.replace("_", " ").lower())
        token = self._current
        self._advance()
        return token

    def _expect_identifier(self) -> Token:
        if self._kind() is not TokenType.IDENTIFIER:
            self._raise_unexpected("an identifier")
        return self._advance()

    def _advance(self) -> Token:
        token = self._current
        if token.kind is not TokenType.EOF:
            self._index += 1
        return token

    def _raise_unexpected(self, expected: str) -> None:
        token = self._current
        self._raise_at(token, expected)

    def _raise_at(self, node: object, expected: str) -> None:
        location = getattr(node, "location", self._current.location)
        value = getattr(node, "value", self._current.value)
        raise PineSyntaxError(f"Expected {expected}, found {value!r}", location)
