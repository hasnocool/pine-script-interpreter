# Architecture

## Processing pipeline

```text
Pine source
    │
    ▼
Lexer ───────► tokens + source locations
    │
    ▼
Parser ──────► syntax tree (AST)
    │
    ▼
Interpreter ◄──► Runtime state
    │              ├─ scoped variables
    │              ├─ historical series
    │              └─ global bar index
    ▼
result per bar
```

## Component responsibilities

### `lexer/`

Converts source text into tokens. The lexer is line-aware and emits `NEWLINE`, `INDENT`, and `DEDENT` tokens because Pine uses indentation instead of braces for blocks.

### `parser/`

Builds immutable AST nodes in `ast_nodes.py`. Semantic validation, such as checking whether a built-in supports the requested qualifier, belongs after parsing rather than in the parser.

### `interpreter/`

Walks the AST. A later implementation may compile the AST to Python bytecode or another instruction set, but the AST can remain the stable front-end contract.

### `runtime/`

Owns state that survives across bar evaluations:

- nested lexical scopes;
- global and per-symbol variables;
- `var` and `varip` persistent declarations;
- historical values indexed by bar;
- the current global bar number.

### `diagnostics.py`

Provides structured source locations and exception types so the CLI and future language-server integration can present consistent messages.

## Execution model

The intended long-term model is:

1. Parse and validate the script once.
2. Create a runtime for each script instance and symbol.
3. For every incoming bar, increment the global bar index.
4. Execute every top-level declaration and statement in source order.
5. Record built-in series at the current bar.
6. Return indicator, plot, alert, and strategy results.

One script instance must never share mutable runtime state with another instance. Persistent variables belong to an instance, not to the parsed syntax tree.

## Extension points

- Add a Pine built-in registry in `runtime/builtins/`.
- Add declaration validation in a separate semantic-analysis pass.
- Add an optimizer only after parser and runtime behavior are well tested.
- Keep standard-library Pine functions separate from Python's `eval` and `exec`; Pine is an embedded domain language, not arbitrary Python.
