"""Compile a small, fixed subset of Python to JavaScript (spec v0.2 §7.3).

This is a deliberately small experiment, not a Python-to-JavaScript
translator: only the constructs listed in :data:`SUPPORTED` compile, and
everything else is a :class:`ClientDSLCompileError`. In particular there are
no imports, no reflection, no file I/O, and no server-only names: code that
touches the server (``open``, ``os``, ``Request``, ``h`` and friends) is
rejected at compile time so it can never leak into a browser bundle.

Semantics that differ between Python and JavaScript are handled explicitly
where the subset allows it: ``//`` is floor division, ``%`` follows the
divisor's sign, negative indexes count from the end, ``and``/``or`` return
operand values, and conditions use Python truthiness (empty lists, dicts
and strings are falsy). Where the subset cannot represent a difference it
says so instead of guessing: see :data:`KNOWN_DIFFERENCES`.
"""

import ast
import json
import math

__all__ = ["ClientDSLCompileError", "compile_client_dsl", "DSL_VERSION", "SUPPORTED", "KNOWN_DIFFERENCES"]

DSL_VERSION = 1

SUPPORTED = (
    "literals: int (within the JavaScript safe-integer range), float, str, "
    "bool, None, list, tuple (as an array), dict (literal str/int/float/bool keys, stringified)",
    "arithmetic: +, -, *, /, //, %, ** and unary -, +",
    "comparisons: ==, !=, <, <=, >, >=, in, not in, is None, is not None, including chains",
    "boolean ops: and, or, not (Python truthiness and operand values)",
    "if/else statements and if/else expressions",
    "for loops over range() or a list, while loops, break, continue",
    "function definitions (positional parameters, literal defaults only), "
    "return, assignment, augmented assignment",
    "calls: len, str, int, float, bool, abs, min, max, list, print, "
    "range (as a for/list-comprehension iterable only), .append()",
    "indexing and slices (step must be None or 1), list comprehensions "
    "with one for clause and an optional if",
)

KNOWN_DIFFERENCES = (
    "str() of a float drops the trailing .0 (Python str(1.0) is '1.0', "
    "JavaScript has one number type, so 1.0 and 1 are the same value)",
    "str() of a string inside a list or dict uses double quotes "
    "(Python uses single quotes there)",
    "int() accepts strings like '3.5' instead of raising, as Python does",
    "very large integers are not supported: values must fit in the "
    "JavaScript safe-integer range",
    "print() writes with console.log, so its exact formatting differs",
    "dict keys are JavaScript object keys: non-string keys are stringified",
    "tuples and lists are both represented as JavaScript arrays, so equality "
    "and mutation cannot distinguish the two types",
    "arithmetic results are JavaScript numbers and are not checked for "
    "safe-integer overflow",
    "division by zero and invalid or out-of-range indexing do not always "
    "raise the same errors as Python",
    "arithmetic and ordering involving strings, lists, booleans or mixed "
    "types can follow JavaScript coercion rather than Python operator semantics",
    "range() is materialized as an array and is limited to 100000 items",
    "non-finite float literals (inf, -inf, nan) are rejected: JSON has no "
    "Infinity or NaN, so they cannot be represented in a browser bundle",
)


class ClientDSLCompileError(ValueError):
    """A source construct is outside the supported DSL subset."""


# Names that only make sense on the server. Rejecting them here (rather than
# leaving an undefined-name error) keeps server code out of browser bundles
# by construction, and makes the reason obvious in the error message.
_SERVER_ONLY = {
    "open", "__import__", "eval", "exec", "compile", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "hasattr", "input",
    "help", "breakpoint", "memoryview", "os", "sys", "socket", "subprocess",
    "pathlib", "requests", "h", "page", "layout", "island", "route",
    "websocket", "Request", "Response", "JSONResponse", "StreamingResponse",
    "create_app", "App",
}

# Builtins the compiler knows how to emit. Anything else called as a
# function is rejected as unsupported.
_BUILTINS = {
    "len", "str", "int", "float", "bool", "abs", "min", "max", "list",
    "dict", "print", "range",
}

_MAX_SAFE_INTEGER = 2**53 - 1
_JS_RESERVED = {
    "arguments", "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "enum", "eval", "export", "extends", "false", "finally",
    "for", "function", "if", "implements", "import", "in", "instanceof", "interface", "let",
    "new", "null", "package", "private", "protected", "public", "return", "static", "super",
    "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield",
}

_HELPERS = """\
function __pyFloorDiv(a, b) { return Math.floor(a / b); }
function __pyMod(a, b) { return ((a % b) + b) % b; }
function __pyBool(v) {
  if (v === null || v === undefined) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string" || Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return !!v;
}
function __pyStr(v) {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (Array.isArray(v)) return "[" + v.map(__pyStr).join(", ") + "]";
  if (typeof v === "object") return "{" + Object.entries(v).map(([k, val]) => JSON.stringify(k) + ": " + __pyStr(val)).join(", ") + "}";
  return String(v);
}
function __pyLen(v) {
  if (typeof v === "string") return Array.from(v).length;
  if (Array.isArray(v)) return v.length;
  if (typeof v === "object" && v !== null) return Object.keys(v).length;
  throw new Error("object has no len()");
}
function __pyEq(a, b) {
  if (a === b) return true;
  if ((typeof a === "number" || typeof a === "boolean") &&
      (typeof b === "number" || typeof b === "boolean")) return Number(a) === Number(b);
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((value, index) => __pyEq(value, b[index]));
  }
  if (a !== null && b !== null && typeof a === "object" && typeof b === "object" &&
      !Array.isArray(a) && !Array.isArray(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length &&
      keys.every(key => Object.prototype.hasOwnProperty.call(b, key) && __pyEq(a[key], b[key]));
  }
  return false;
}
function __pyContains(v, item) {
  if (typeof v === "string") {
    if (typeof item !== "string") throw new Error("string membership requires a string");
    return v.includes(item);
  }
  if (Array.isArray(v)) return v.some(value => __pyEq(value, item));
  if (typeof v === "object" && v !== null) return Object.prototype.hasOwnProperty.call(v, item);
  throw new Error("argument of type is not iterable");
}
function __pyInt(v) { return Math.trunc(Number(v)); }
function __pyRange(a, b, c) {
  let start = 0, stop = a, step = 1;
  if (b !== undefined) { start = a; stop = b; }
  if (c !== undefined) step = c;
  if (![start, stop, step].every(Number.isSafeInteger)) throw new Error("range() requires safe integers");
  if (step === 0) throw new Error("range() arg 3 must not be zero");
  const count = Math.max(0, Math.ceil((stop - start) / step));
  if (count > 100000) throw new Error("range() exceeds the DSL item limit");
  const out = [];
  if (step > 0) { for (let i = start; i < stop; i += step) out.push(i); }
  else if (step < 0) { for (let i = start; i > stop; i += step) out.push(i); }
  return out;
}
function __pyGet(v, i) {
  if (typeof v === "string") v = Array.from(v);
  if (Array.isArray(v)) {
    if (!Number.isInteger(i)) throw new Error("sequence index must be an integer");
    if (i < 0) i += v.length;
    if (i < 0 || i >= v.length) throw new Error("sequence index out of range");
    return v[i];
  }
  if (typeof v === "object" && v !== null && Object.prototype.hasOwnProperty.call(v, i)) return v[i];
  throw new Error("key not found");
}
function __pySlice(v, a, b) {
  const string = typeof v === "string";
  if (string) v = Array.from(v);
  const result = v.slice(a === null || a === undefined ? undefined : a, b === null || b === undefined ? undefined : b);
  return string ? result.join("") : result;
}
"""


class _Compiler:
    def __init__(self):
        self.lines = [_HELPERS.rstrip("\n")]
        self.scopes = []  # stack of sets of names declared in each scope
        self.module_functions = set()
        self.in_function = 0

    # -- errors ---------------------------------------------------------
    def fail(self, node, message):
        location = f"line {node.lineno}" if isinstance(node, ast.AST) else ""
        raise ClientDSLCompileError(f"{location}: {message}" if location else message)

    # -- scope bookkeeping ----------------------------------------------
    def declare(self, name):
        self.scopes[-1].add(name)

    def is_defined(self, name):
        return any(name in scope for scope in reversed(self.scopes))

    def emit(self, indent, text):
        self.lines.append("  " * indent + text)

    # -- entry point ------------------------------------------------------
    def compile(self, source):
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ClientDSLCompileError(f"invalid Python syntax: {exc}") from exc
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.module_functions.add(node.name)
        self.scopes.append(set(self.module_functions))
        for node in tree.body:
            self.statement(node, 0)
        self.scopes.pop()
        return "\n".join(self.lines) + "\n"

    # -- statements -------------------------------------------------------
    def statement(self, node, indent):
        if isinstance(node, ast.FunctionDef):
            self.function_def(node, indent)
        elif isinstance(node, ast.Assign):
            self.assign(node, indent)
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                self.fail(node, "annotated assignment needs a value")
            self.assign_value(node.target, node.value, indent)
        elif isinstance(node, ast.AugAssign):
            self.aug_assign(node, indent)
        elif isinstance(node, ast.If):
            self.if_statement(node, indent)
        elif isinstance(node, ast.For):
            self.for_loop(node, indent)
        elif isinstance(node, ast.While):
            self.emit(indent, f"while (__pyBool({self.expr(node.test)})) {{")
            for child in node.body:
                self.statement(child, indent + 1)
            self.emit(indent, "}")
            if node.orelse:
                self.fail(node, "while/else is not supported")
        elif isinstance(node, ast.Return):
            if not self.in_function:
                self.fail(node, "return outside a function is not supported")
            self.emit(indent, f"return {self.expr(node.value)};" if node.value is not None else "return;")
        elif isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return  # docstring
            self.emit(indent, self.expr(node.value) + ";")
        elif isinstance(node, ast.Pass):
            self.emit(indent, ";")
        elif isinstance(node, ast.Break):
            self.emit(indent, "break;")
        elif isinstance(node, ast.Continue):
            self.emit(indent, "continue;")
        else:
            self.fail(node, f"{type(node).__name__} is not in the supported subset")

    def if_statement(self, node, indent):
        # Flatten an if/elif/else chain so each block is closed exactly once.
        parts = []
        current = node
        else_body = None
        while True:
            parts.append((current.test, current.body))
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            else_body = current.orelse or None
            break
        for index, (test, body) in enumerate(parts):
            keyword = "if" if index == 0 else "} else if"
            self.emit(indent, f"{keyword} (__pyBool({self.expr(test)})) {{")
            for child in body:
                self.statement(child, indent + 1)
        if else_body is not None:
            self.emit(indent, "} else {")
            for child in else_body:
                self.statement(child, indent + 1)
        self.emit(indent, "}")

    def check_name(self, name, node):
        if name.startswith("__py"):
            self.fail(node, "names starting with __py are reserved for the compiler")
        if name in _SERVER_ONLY:
            self.fail(node, f"{name!r} is server-only and cannot be compiled for the browser")
        if name in _BUILTINS:
            self.fail(node, f"{name!r} is a reserved DSL builtin")
        if name in _JS_RESERVED:
            self.fail(node, f"{name!r} is reserved in JavaScript")

    def function_def(self, node, indent):
        self.check_name(node.name, node)
        if node.decorator_list:
            self.fail(node, "decorators are not supported")
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            self.fail(node, "only positional parameters are supported")
        if node.args.posonlyargs:
            self.fail(node, "positional-only parameters are not supported")
        defaults = node.args.defaults
        params = [arg.arg for arg in node.args.args]
        for name in params:
            self.check_name(name, node)
        if len(defaults) > len(params):
            self.fail(node, "invalid parameter defaults")
        rendered = []
        for index, name in enumerate(params):
            default_index = index - (len(params) - len(defaults))
            if default_index >= 0:
                rendered.append(f"{name} = {self.literal(defaults[default_index])}")
            else:
                rendered.append(name)
        self.emit(indent, f"function {node.name}({', '.join(rendered)}) {{")
        self.scopes.append(set(params))
        self.in_function += 1
        try:
            for child in node.body:
                if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) \
                        and isinstance(child.value.value, str):
                    continue  # docstring
                self.statement(child, indent + 1)
        finally:
            self.in_function -= 1
            self.scopes.pop()
        self.emit(indent, "}")

    def literal(self, node):
        if not isinstance(node, ast.Constant):
            self.fail(node, "parameter defaults must be literals")
        return self.constant(node)

    def assign(self, node, indent):
        if len(node.targets) != 1:
            self.fail(node, "multiple assignment targets are not supported")
        self.assign_value(node.targets[0], node.value, indent)

    def assign_value(self, target, value, indent):
        if not isinstance(target, ast.Name):
            self.fail(target, "only a plain name can be assigned to")
        self.check_name(target.id, target)
        code = self.expr(value)
        if target.id in self.scopes[-1]:
            self.emit(indent, f"{target.id} = {code};")
        else:
            self.declare(target.id)
            self.emit(indent, f"let {target.id} = {code};")

    def aug_assign(self, node, indent):
        if not isinstance(node.target, ast.Name):
            self.fail(node.target, "only a plain name can be assigned to")
        if not self.is_defined(node.target.id):
            self.fail(node.target, f"undefined name: {node.target.id!r}")
        target = node.target.id
        value = self.expr(node.value)
        if isinstance(node.op, ast.FloorDiv):
            self.emit(indent, f"{target} = __pyFloorDiv({target}, {value});")
        elif isinstance(node.op, ast.Mod):
            self.emit(indent, f"{target} = __pyMod({target}, {value});")
        elif isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.BitAnd,
                                  ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)):
            op = {"Add": "+", "Sub": "-", "Mult": "*", "Div": "/", "Pow": "**",
                  "BitAnd": "&", "BitOr": "|", "BitXor": "^", "LShift": "<<",
                  "RShift": ">>"}[type(node.op).__name__]
            self.emit(indent, f"{target} {op}= {value};")
        else:
            self.fail(node, f"augmented operator {type(node.op).__name__} is not supported")

    def for_loop(self, node, indent):
        if node.orelse:
            self.fail(node, "for/else is not supported")
        if not isinstance(node.target, ast.Name):
            self.fail(node.target, "for-loop target must be a plain name")
        self.check_name(node.target.id, node.target)
        iterable = self.iterable(node.iter)
        target = node.target.id
        if target in self.scopes[-1]:
            header = f"for ({target} of {iterable}) {{"
        else:
            self.declare(target)
            header = f"let {target};\n{'  ' * indent}for ({target} of {iterable}) {{"
        self.emit(indent, header)
        for child in node.body:
            self.statement(child, indent + 1)
        self.emit(indent, "}")

    def iterable(self, node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
            return self.range_call(node)
        return self.expr(node)

    def range_call(self, node):
        if node.keywords:
            self.fail(node, "range() takes no keyword arguments")
        if not 1 <= len(node.args) <= 3:
            self.fail(node, "range() takes 1 to 3 arguments")
        return "__pyRange(" + ", ".join(self.expr(arg) for arg in node.args) + ")"

    # -- expressions ------------------------------------------------------
    def expr(self, node):
        method = getattr(self, "expr_" + type(node).__name__, None)
        if method is None:
            self.fail(node, f"{type(node).__name__} is not in the supported subset")
        return method(node)

    def constant(self, node):
        value = node.value
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, int):
            if abs(value) > _MAX_SAFE_INTEGER:
                self.fail(node, "integer is outside the JavaScript safe-integer range")
            return str(value)
        if isinstance(value, float):
            # json.dumps(float("inf")) emits the bare word Infinity, which
            # is not valid JSON (JSON.parse rejects it) and would silently
            # change meaning in a browser bundle, so reject non-finite
            # floats at compile time instead of emitting them.
            if not math.isfinite(value):
                self.fail(node, "non-finite float literals (inf, -inf, nan) are not supported")
            return json.dumps(value)
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=True)
        self.fail(node, f"literal of type {type(value).__name__} is not supported")

    def expr_Constant(self, node):
        return self.constant(node)

    def expr_Name(self, node):
        if node.id in _SERVER_ONLY:
            self.fail(node, f"{node.id!r} is server-only and cannot be compiled for the browser")
        if isinstance(node.ctx, ast.Store):
            self.fail(node, "unexpected assignment target")
        if node.id == "range":
            self.fail(node, "range() is only supported as a for-loop or comprehension iterable")
        if node.id in _BUILTINS:
            self.fail(node, "DSL builtins are only supported as direct calls")
        self.check_name(node.id, node)
        if not self.is_defined(node.id):
            self.fail(node, f"undefined name: {node.id!r}")
        return node.id

    def expr_List(self, node):
        return "[" + ", ".join(self.expr(item) for item in node.elts) + "]"

    def expr_Tuple(self, node):
        return "[" + ", ".join(self.expr(item) for item in node.elts) + "]"

    def expr_Dict(self, node):
        parts = []
        for key, value in zip(node.keys, node.values):
            if key is None:
                self.fail(node, "dict unpacking is not supported")
            if not isinstance(key, ast.Constant) or not isinstance(key.value, (str, int, float, bool)):
                self.fail(key, "dict keys must be str, int, float or bool literals")
            parts.append(f"[{json.dumps(str(key.value), ensure_ascii=True)}]: {self.expr(value)}")
        return "{" + ", ".join(parts) + "}"

    def expr_BinOp(self, node):
        left = self.expr(node.left)
        right = self.expr(node.right)
        if isinstance(node.op, ast.FloorDiv):
            return f"__pyFloorDiv({left}, {right})"
        if isinstance(node.op, ast.Mod):
            return f"__pyMod({left}, {right})"
        ops = {"Add": "+", "Sub": "-", "Mult": "*", "Div": "/", "Pow": "**",
               "BitAnd": "&", "BitOr": "|", "BitXor": "^", "LShift": "<<", "RShift": ">>"}
        op = ops.get(type(node.op).__name__)
        if op is None:
            self.fail(node, f"operator {type(node.op).__name__} is not supported")
        return f"({left} {op} {right})"

    def expr_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return f"(!__pyBool({self.expr(node.operand)}))"
        if isinstance(node.op, ast.USub):
            return f"(-{self.expr(node.operand)})"
        if isinstance(node.op, ast.UAdd):
            return f"(+{self.expr(node.operand)})"
        self.fail(node, f"unary operator {type(node.op).__name__} is not supported")

    def expr_BoolOp(self, node):
        # Evaluate each operand exactly once (an IIFE binds it), keep
        # Python's operand-value result and short-circuiting.
        parts = [self.expr(value) for value in node.values]
        result = parts[0]
        for part in parts[1:]:
            if isinstance(node.op, ast.And):
                result = f"((__pyv) => (__pyBool(__pyv) ? {part} : __pyv))({result})"
            elif isinstance(node.op, ast.Or):
                result = f"((__pyv) => (__pyBool(__pyv) ? __pyv : {part}))({result})"
            else:
                self.fail(node, f"boolean operator {type(node.op).__name__} is not supported")
        return result

    def expr_Compare(self, node):
        # Bind each operand once and evaluate later operands only when all
        # previous comparisons succeeded, matching Python's chained tests.
        pieces = [f"const __pycmp0 = {self.expr(node.left)};"]
        for index, (op, comparator) in enumerate(zip(node.ops, node.comparators), start=1):
            right = f"__pycmp{index}"
            left = f"__pycmp{index - 1}"
            pieces.append(f"const {right} = {self.expr(comparator)};")
            comparison = self._comparison(node, op, comparator, left, right)
            pieces.append(f"if (!({comparison})) return false;")
        pieces.append("return true;")
        return "(() => { " + " ".join(pieces) + " })()"

    def _comparison(self, node, op, comparator, left, right):
        if isinstance(op, ast.Eq):
            return f"__pyEq({left}, {right})"
        if isinstance(op, ast.NotEq):
            return f"!__pyEq({left}, {right})"
        if isinstance(op, ast.Lt):
            return f"({left} < {right})"
        if isinstance(op, ast.LtE):
            return f"({left} <= {right})"
        if isinstance(op, ast.Gt):
            return f"({left} > {right})"
        if isinstance(op, ast.GtE):
            return f"({left} >= {right})"
        if isinstance(op, ast.In):
            return f"__pyContains({right}, {left})"
        if isinstance(op, ast.NotIn):
            return f"(!__pyContains({right}, {left}))"
        if isinstance(op, (ast.Is, ast.IsNot)):
            if not (isinstance(comparator, ast.Constant) and comparator.value is None):
                self.fail(node, "only 'is None' / 'is not None' are supported")
            return f"({left} === null)" if isinstance(op, ast.Is) else f"({left} !== null)"
        self.fail(node, f"comparison {type(op).__name__} is not supported")

    def expr_IfExp(self, node):
        return (f"(__pyBool({self.expr(node.test)}) ? {self.expr(node.body)} "
                f": {self.expr(node.orelse)})")

    def expr_Call(self, node):
        if node.keywords:
            self.fail(node, "keyword arguments are not supported")
        if isinstance(node.func, ast.Attribute):
            if node.func.attr != "append" or len(node.args) != 1:
                self.fail(node, "only .append(value) method calls are supported")
            return (f"((__pylist, __pyvalue) => {{ __pylist.push(__pyvalue); return null; }})"
                    f"({self.expr(node.func.value)}, {self.expr(node.args[0])})")
        if not isinstance(node.func, ast.Name):
            self.fail(node, "only plain function calls are supported")
        name = node.func.id
        if name in _SERVER_ONLY:
            self.fail(node, f"{name!r} is server-only and cannot be compiled for the browser")
        if name == "range":
            self.fail(node, "range() is only supported as a for-loop or comprehension iterable")
        args = ", ".join(self.expr(arg) for arg in node.args)
        if name == "len":
            if len(node.args) != 1:
                self.fail(node, "len() takes exactly one argument")
            return f"__pyLen({args})"
        if name == "str":
            if len(node.args) != 1:
                self.fail(node, "str() takes exactly one argument")
            return f"__pyStr({args})"
        if name == "int":
            if len(node.args) != 1:
                self.fail(node, "int() takes exactly one argument")
            return f"__pyInt({args})"
        if name == "float":
            if len(node.args) != 1:
                self.fail(node, "float() takes exactly one argument")
            return f"Number({args})"
        if name == "bool":
            if len(node.args) != 1:
                self.fail(node, "bool() takes exactly one argument")
            return f"__pyBool({args})"
        if name == "abs":
            if len(node.args) != 1:
                self.fail(node, "abs() takes exactly one argument")
            return f"Math.abs({args})"
        if name in {"min", "max"}:
            if len(node.args) < 1:
                self.fail(node, f"{name}() takes at least one argument")
            return f"Math.{name}({args})"
        if name == "list":
            if len(node.args) != 1:
                self.fail(node, "list() takes exactly one argument")
            return f"[...{args}]"
        if name == "dict":
            if node.args:
                self.fail(node, "only dict() with no arguments is supported")
            return "{}"
        if name == "print":
            return f"(console.log({args}), null)"
        if self.is_defined(name):
            return f"{name}({args})"
        self.fail(node, f"{name!r} is not in the supported subset")

    def expr_Subscript(self, node):
        value = self.expr(node.value)
        if isinstance(node.slice, ast.Slice):
            if node.slice.step is not None and not (
                    isinstance(node.slice.step, ast.Constant) and node.slice.step.value in (None, 1)):
                self.fail(node.slice, "slices with a step other than 1 are not supported")
            lower = self.expr(node.slice.lower) if node.slice.lower is not None else "null"
            upper = self.expr(node.slice.upper) if node.slice.upper is not None else "null"
            return f"__pySlice({value}, {lower}, {upper})"
        return f"__pyGet({value}, {self.expr(node.slice)})"

    def expr_ListComp(self, node):
        if len(node.generators) != 1:
            self.fail(node, "only one for clause is supported in a comprehension")
        generator = node.generators[0]
        if generator.is_async or generator.ifs and len(generator.ifs) > 1:
            self.fail(node, "only one if clause is supported in a comprehension")
        if not isinstance(generator.target, ast.Name):
            self.fail(node, "comprehension target must be a plain name")
        self.check_name(generator.target.id, generator.target)
        iterable = self.iterable(generator.iter)
        target = generator.target.id
        self.scopes.append({target})
        try:
            element = self.expr(node.elt)
            if generator.ifs:
                condition = self.expr(generator.ifs[0])
                return f"({iterable}.filter(({target}) => __pyBool({condition})).map(({target}) => {element}))"
            return f"({iterable}.map(({target}) => {element}))"
        finally:
            self.scopes.pop()

    def expr_Attribute(self, node):
        self.fail(node, "attribute access is not supported (only .append() calls are)")


def compile_client_dsl(source):
    """Compile a small Python subset to JavaScript.

    Raises :class:`ClientDSLCompileError` for any construct outside the
    fixed supported list (see :data:`SUPPORTED`), including imports,
    reflection, file I/O and server-only names.
    """
    if not isinstance(source, str):
        raise TypeError("client DSL source must be str")
    return _Compiler().compile(source)
