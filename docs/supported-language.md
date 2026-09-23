# Supported language

The syntax frontend is designed around the Pine v1-v6 scripts in the public
TradingView script archive. Runtime support is intentionally narrower and is
listed separately below.

## Parser coverage

### Lexical syntax

- Four-space indentation with tab expansion and explicit indent/dedent markers
- Wrapped expressions inside `()`, `[]`, and `{}` and after expression operators
- Visual continuation and indentation forms, including realigned ternary/switch cases, operator-led lines, `to`/`by` clauses, tab-indented legacy blocks, and stray visual dedents
- `//` comments, blank/comment-only lines, legacy semicolons, and comma-separated declarations
- Double-quoted strings, legacy single-quoted strings, and common escapes
- Decimal, floating-point, and hexadecimal integer literals
- Six- and eight-digit hexadecimal color literals
- `//@version` and other annotation comments (treated as comments)

### Expressions

- Numbers, strings, booleans, `na`, colors, and identifiers
- Unary `+`, `-`, and `not`
- Arithmetic, comparison, boolean, and power operators
- Right-associative conditional (`condition ? value : fallback`) expressions
- Parenthesized expressions
- Function and method calls, namespaces, and named arguments
- Generic calls such as `array.new<float>()`
- Member access such as `request.security` and `table.cell`
- Historical references such as `close[1]`
- Array/tuple expressions such as `[signal, value]`
- `if` and `switch` expressions

### Declarations and statements

- Inferred, typed, `const`/`input`/`simple`/`series`-qualified declarations
- `var` and `varip` declarations and exported constants
- `=`, `:=`, and compound assignments
- Tuple destructuring declarations
- `if` / `else if` / `else` blocks
- Counted and collection `for` loops
- `while`, `break`, and `continue`
- `switch` statements and expressions, including multiline case bodies
- User functions, methods, exported functions, parameters, and defaults
- `import` declarations with optional aliases
- Enums with optional explicit values
- User-defined types, fields, and methods
- Indicator, strategy, and library declaration calls

## Runtime coverage

The tree-walking runtime currently implements:

- Integer, float, boolean, and string values
- Basic declarations, arithmetic, comparisons, and boolean expressions
- Scoped `if` / `else` execution
- Source-aware runtime errors

## Not yet implemented in the runtime

- Market-data and built-in namespaces (`ta`, `request`, `strategy`, and others)
- User-defined function and method invocation
- Time-series, history-referencing, and `bar_index` semantics
- User-defined types, enums, arrays, matrices, and maps
- Persistent `var` / `varip` state across bars
- Indicator, strategy, library, import, and drawing behavior

Successful parsing does not imply that these runtime features are implemented.
