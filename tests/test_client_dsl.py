"""Output-equivalence tests for the Python client DSL (spec v0.2 §7.3).

Each case evaluates the same expression in Python (via exec/eval) and in
the compiled JavaScript (via node) and asserts identical JSON results.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pynext.client_dsl import ClientDSLCompileError, compile_client_dsl

NODE = shutil.which("node")


def run_node(js):
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
        handle.write(js)
        path = handle.name
    try:
        result = subprocess.run([NODE, path], capture_output=True, text=True, encoding="utf-8", timeout=15)
    finally:
        Path(path).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def check(source, expressions):
    """Assert Python and compiled-JS results agree for each expression pair."""
    if NODE is None:
        pytest.skip("node is required for DSL equivalence tests")
    namespace = {}
    exec(source, namespace)
    js = compile_client_dsl(source)
    assert "import" not in js and "require(" not in js
    for py_expr, js_expr in expressions:
        expected = eval(py_expr, namespace)
        actual = json.loads(run_node(js + f"\nconsole.log(JSON.stringify({js_expr}));"))
        assert actual == expected, (py_expr, expected, actual)


def test_arithmetic_matches_python_including_floor_div_and_modulo():
    source = (
        "def calc(a, b):\n"
        "    return [a + b, a - b, a * b, a / b, a // b, a % b, a ** 2, -a]\n"
    )
    check(source, [
        ("calc(7, 3)", "calc(7, 3)"),
        ("calc(-7, 3)", "calc(-7, 3)"),
        ("calc(7, -3)", "calc(7, -3)"),
        ("calc(5, 2)", "calc(5, 2)"),
    ])


def test_comparisons_and_boolean_ops_return_operand_values():
    source = (
        "def pick(a, b):\n"
        "    return [a == b, a != b, a < b, 1 in [1, 2], 5 not in [1, 2]]\n"
        "def logic(a, b):\n"
        "    return [a and b, a or b, not a]\n"
        "def check_none(x):\n"
        "    return [x is None, x is not None]\n"
    )
    check(source, [
        ("pick(2, 3)", "pick(2, 3)"),
        ("logic(0, 'x')", "logic(0, 'x')"),
        ("logic([], [1])", "logic([], [1])"),
        ("logic('a', '')", "logic('a', '')"),
        ("check_none(None)", "check_none(null)"),
        ("check_none(1)", "check_none(1)"),
    ])


def test_if_else_and_if_expression():
    source = (
        "def classify(n):\n"
        "    if n < 0:\n"
        "        return 'neg'\n"
        "    elif n == 0:\n"
        "        return 'zero'\n"
        "    else:\n"
        "        return 'pos'\n"
        "def label(n):\n"
        "    return 'even' if n % 2 == 0 else 'odd'\n"
    )
    check(source, [
        ("classify(-1)", "classify(-1)"),
        ("classify(0)", "classify(0)"),
        ("classify(4)", "classify(4)"),
        ("label(3)", "label(3)"),
        ("label(8)", "label(8)"),
    ])


def test_loops_and_accumulation():
    source = (
        "def total(n):\n"
        "    s = 0\n"
        "    for i in range(n):\n"
        "        s += i\n"
        "    return s\n"
        "def countdown(n):\n"
        "    out = []\n"
        "    while n > 0:\n"
        "        out.append(n)\n"
        "        n -= 1\n"
        "    return out\n"
        "def stepped():\n"
        "    return [i for i in range(0, 10, 3)]\n"
    )
    check(source, [
        ("total(10)", "total(10)"),
        ("total(0)", "total(0)"),
        ("countdown(3)", "countdown(3)"),
        ("stepped()", "stepped()"),
    ])


def test_lists_strings_dicts_and_indexing():
    source = (
        "def ops(xs):\n"
        "    xs.append(4)\n"
        "    return [len(xs), xs[0], xs[-1], xs[1:3], xs[2:], len('hello'), 'ell' in 'hello']\n"
        "def lookup(d, key):\n"
        "    return [d[key], len(d), key in d]\n"
        "def squares(n):\n"
        "    return [x * x for x in range(n) if x % 2 == 0]\n"
    )
    check(source, [
        ("ops([1, 2, 3])", "ops([1, 2, 3])"),
        ("lookup({'a': 1, 'b': 2}, 'b')", "lookup({a: 1, b: 2}, 'b')"),
        ("squares(6)", "squares(6)"),
    ])


def test_conversions_and_functions():
    source = (
        "def double(x):\n"
        "    return x * 2\n"
        "def convert(x):\n"
        "    return [str(x), int(3.7), float('1.5'), abs(-4), min(3, 1, 2), max(3, 1, 2), bool(0), bool('x')]\n"
        "def greet(name='world'):\n"
        "    return 'hi ' + name\n"
    )
    check(source, [
        ("double(21)", "double(21)"),
        ("convert(42)", "convert(42)"),
        ("greet()", "greet()"),
        ("greet('py')", "greet('py')"),
    ])


def test_chained_comparisons_short_circuit_and_evaluate_once():
    source = (
        "def record(seen, value):\n"
        "    seen.append(value)\n"
        "    return value\n"
        "def chain(last):\n"
        "    seen = []\n"
        "    result = record(seen, 1) < record(seen, 2) < record(seen, last)\n"
        "    return [result, seen]\n"
        "def stopped():\n"
        "    seen = []\n"
        "    result = record(seen, 2) < record(seen, 1) < record(seen, 3)\n"
        "    return [result, seen]\n"
    )
    check(source, [("chain(3)", "chain(3)"), ("chain(0)", "chain(0)"), ("stopped()", "stopped()")])


def test_collection_equality_membership_and_unicode_match_python():
    source = (
        "def values():\n"
        "    d = {'__proto__': [1, 2], 'x': {'a': True}}\n"
        "    return [d == {'x': {'a': 1}, '__proto__': [1, 2]}, [1, [2]] == [1, [2]],\n"
        "            [1] in [[1], [2]], 'toString' in d, '__proto__' in d, d['__proto__'],\n"
        "            len('a😀b'), 'a😀b'[1], 'a😀b'[-2:], True == 1]\n"
    )
    check(source, [("values()", "values()")])


def test_append_expression_returns_none_and_indices_obey_bounds():
    source = (
        "def appended():\n"
        "    values = [1]\n"
        "    result = values.append(2)\n"
        "    return [result is None, values[-1]]\n"
    )
    check(source, [("appended()", "appended()")])
    if NODE is None:
        pytest.skip("node is required for DSL boundary tests")
    js = compile_client_dsl("def get(xs):\n    return xs[3]\n")
    assert run_node(js + "\ntry { get([1]); } catch (error) { console.log('out-of-range'); }") == "out-of-range"


def test_range_is_bounded_before_allocating():
    if NODE is None:
        pytest.skip("node is required for DSL boundary tests")
    js = compile_client_dsl("def values():\n    return [x for x in range(100001)]\n")
    assert run_node(js + "\ntry { values(); } catch (error) { console.log('bounded'); }") == "bounded"


@pytest.mark.parametrize("source,match", [
    ("import os\n", "not in the supported subset"),
    ("x = open('f')\n", "server-only"),
    ("x = eval('1')\n", "server-only"),
    ("x = h('p')\n", "server-only"),
    ("x = getattr(o, 'a')\n", "server-only"),
    ("class C:\n    pass\n", "not in the supported subset"),
    ("f = lambda x: x\n", "not in the supported subset"),
    ("try:\n    x = 1\nexcept:\n    pass\n", "not in the supported subset"),
    ("x = f'{1}'\n", "not in the supported subset"),
    ("x = o.attr\n", "attribute access is not supported"),
    ("x = unknown_name\n", "undefined name"),
    ("x = Request()\n", "server-only"),
    ("def f(**kwargs):\n    return 1\n", "only positional parameters"),
    ("async def f():\n    pass\n", "not in the supported subset"),
    ("with open('f') as f:\n    pass\n", "not in the supported subset"),
    ("x = {k: v for k, v in []}\n", "not in the supported subset"),
    ("x = 9007199254740993\n", "safe-integer range"),
    ("x = 1 is 2\n", "only 'is None'"),
    ("x = 1\nx += 1\ndef f():\n    global x\n", "not in the supported subset"),
    ("def __pyBool(x):\n    return x\n", "reserved for the compiler"),
    ("let = 1\n", "reserved in JavaScript"),
    ("def widget(interface):\n    return interface\n", "reserved in JavaScript"),
    ("open = 'local'\n", "server-only"),
    ("def widget(len):\n    return len\n", "reserved DSL builtin"),
    ("f = len\n", "only supported as direct calls"),
])
def test_unsupported_constructs_fail_compile(source, match):
    with pytest.raises(ClientDSLCompileError, match=match):
        compile_client_dsl(source)


def test_top_level_statements_compile_and_run():
    if NODE is None:
        pytest.skip("node is required for DSL equivalence tests")
    js = compile_client_dsl("total = 0\nfor i in range(5):\n    total += i\n")
    assert json.loads(run_node(js + "\nconsole.log(JSON.stringify(total));")) == 10
