"""Example input for the Python client DSL (spec v0.2 §7.3).

This file is not an island and is not bundled by the build: it shows what
``pynext.client_dsl.compile_client_dsl`` accepts. Compile it with::

    from pynext.client_dsl import compile_client_dsl
    from pathlib import Path
    js = compile_client_dsl(Path("client_dsl_example.py").read_text())

Only the fixed subset in ``pynext.client_dsl.SUPPORTED`` is allowed: no
imports, no reflection, no file I/O, and no server-only names.
"""


def score_label(points):
    if points >= 100:
        return "gold"
    elif points >= 50:
        return "silver"
    else:
        return "bronze"


def total(points):
    result = 0
    for value in points:
        result += value
    return result


def top_two(points):
    ranked = [x for x in points if x >= 50]
    return ranked[:2]
