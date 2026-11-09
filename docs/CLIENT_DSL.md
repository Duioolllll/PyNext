# Python client DSL contract, version 1

`pynext.client_dsl.DSL_VERSION` identifies the standalone compiler contract.
`compile_client_dsl(source)` accepts a Python source string and returns JavaScript
source; unsupported syntax raises `ClientDSLCompileError`. It does not import
the source or automatically bundle it into an island. Only compile code intended
to be public. The compiler is not a sandbox for executing untrusted code.

The supported syntax and types are listed in `pynext.client_dsl.SUPPORTED`.
They include JSON-like literals, arithmetic, comparisons including chains,
short-circuit boolean operators, conditionals, loops, positional functions,
indexing, slices and single-clause list comprehensions. Imports, classes,
reflection, file access, asynchronous code, and server-only names are rejected.
Compiler helper names beginning with `__py`, JavaScript reserved words and DSL
builtin names cannot be declared. Builtins can only be used in direct calls.
`range` is materialized and limited to 100,000 safe-integer items.

The runtime uses JavaScript numbers, arrays and object keys. As a result, Python
tuples and lists cannot be distinguished, dictionary keys are stringified,
arithmetic and comparisons on mixed or nonnumeric types may use JavaScript
coercion, numeric operations do not enforce the safe-integer bound, and some
conversion and error behaviors differ. The maintained list is
`pynext.client_dsl.KNOWN_DIFFERENCES`; it includes string formatting, `int`
parsing and exceptions. Applications must not depend on unsupported Python
behavior or use DSL output as a security boundary.

Non-finite float literals are rejected at compile time. This does not
enforce finite runtime values: `float("Infinity")`, `float("nan")`, or
arithmetic such as `1e308 * 1e308` can still produce JavaScript Infinity
or NaN. Runtime results are not checked for finiteness.

`tests/test_client_dsl.py` executes representative source in Python and the
compiled result in Node, including Unicode, collection equality, chained
comparison evaluation, reserved names and range limits. Those checks establish
the tested subset and do not imply full Python language compatibility.
