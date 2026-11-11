"""Escape-hatch tests for S-P1-10 (explicit SQL / HTML escape hatches).

Rules under test (see docs/SECURITY.md):

* SQL: values are passed separately from the statement, never
  interpolated. ``raw_sql()`` is the only explicit raw-SQL spelling.
* HTML: escaping is ON by default; only ``mark_safe()`` opts out,
  and only for element children (attribute values stay escaped).
"""
import json
import sqlite3

import pytest

from pynext import _native, h, mark_safe, SafeString
from pynext.cache import SQLiteCacheBackend
from pynext.sql import RawSQL, quote_identifier, raw_sql, validate_ddl_definition


# ---------------------------------------------------------------- HTML

def test_plain_strings_become_escaped_text_nodes():
    tree = h("p", {}, "<script>alert(1)</script>")
    assert tree["children"] == [{"kind": "text", "value": "<script>alert(1)</script>"}]
    assert _native.render(json.dumps(tree)) == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"


def test_renderer_escapes_text_and_attribute_values_by_default():
    rendered = _native.render(json.dumps(h("p", {"title": '"<&'}, "<b>&\"'")))
    assert rendered == '<p title="&quot;&lt;&amp;">&lt;b&gt;&amp;&quot;&#x27;</p>'
    assert "<b>" not in rendered and "<script>" not in rendered


def test_mark_safe_produces_raw_node_only_for_wrapped_values():
    tree = h("div", {}, mark_safe("<b>trusted</b>"), "plain <i>text</i>")
    assert tree["children"] == [
        {"kind": "raw", "value": "<b>trusted</b>"},
        {"kind": "text", "value": "plain <i>text</i>"},
    ]


def test_mark_safe_requires_string_and_returns_safe_string():
    safe = mark_safe("<b>x</b>")
    assert isinstance(safe, SafeString) and isinstance(safe, str)
    assert safe.__html__() == "<b>x</b>"
    for bad in (None, 42, b"<b>x</b>", ["<b>x</b>"]):
        with pytest.raises(TypeError, match="mark_safe\\(\\) requires a string"):
            mark_safe(bad)


def test_mark_safe_does_not_escape_attributes():
    # Attribute values are always escaped, even when wrapped: there is
    # no safe raw form for breaking out of href="...".
    rendered = _native.render(json.dumps(
        h("a", {"href": mark_safe("https://example.com/?a=\"<b>")}, "x")))
    assert rendered == '<a href="https://example.com/?a=&quot;&lt;b&gt;">x</a>'


def test_raw_nodes_are_rejected_by_shared_models():
    from pynext import island

    with pytest.raises(ValueError):
        island("x", hydrate=True, model={"kind": "raw", "value": "<b>x</b>"},
               props={})


# ---------------------------------------------------------------- SQL

def test_raw_sql_binds_params_instead_of_interpolating():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    raw_sql("INSERT INTO t (name) VALUES (?)", ("alice",)).execute(conn)
    # An injection payload passed as a bound value stays a literal.
    payload = "x' OR '1'='1"
    raw_sql("INSERT INTO t (name) VALUES (?)", (payload,)).execute(conn)
    rows = raw_sql("SELECT name FROM t WHERE name = ?", (payload,)).execute(conn).fetchall()
    assert rows == [(payload,)]
    rows = raw_sql("SELECT name FROM t WHERE name = ?", ("alice",)).execute(conn).fetchall()
    assert rows == [("alice",)]
    assert raw_sql("SELECT COUNT(*) FROM t").execute(conn).fetchone() == (2,)


def test_raw_sql_rejects_bad_shapes():
    with pytest.raises(TypeError, match="must be a string"):
        raw_sql(42)
    with pytest.raises(TypeError, match="must be a tuple, list, dict, or None"):
        raw_sql("SELECT 1", params="oops")
    assert isinstance(raw_sql("SELECT 1"), RawSQL)
    assert raw_sql("SELECT 1").params == ()


def test_quote_identifier_accepts_only_plain_identifiers():
    assert quote_identifier("entries") == '"entries"'
    assert quote_identifier("_col9") == '"_col9"'
    for bad in ("entries; DROP TABLE entries;--", "a b", 'a"b', "a-b", "", None, 42):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            quote_identifier(bad)


def test_validate_ddl_definition_accepts_only_fixed_shapes():
    assert validate_ddl_definition("REAL") == "REAL"
    assert validate_ddl_definition("TEXT NOT NULL DEFAULT '[]'") == "TEXT NOT NULL DEFAULT '[]'"
    for bad in ("REAL; DROP TABLE entries;--", "TEXT DEFAULT (SELECT 1)", "VARCHAR(10)",
                "REAL NOT NULL DEFAULT now()", "TEXT DEFAULT 'x' CHECK (1)"):
        with pytest.raises(ValueError, match="Invalid DDL column definition"):
            validate_ddl_definition(bad)


def test_cache_ensure_column_uses_validated_identifiers(tmp_path):
    cache = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    # Normal path still migrates the columns.
    columns = [row[1] for row in cache._conn.execute("PRAGMA table_info(entries)")]
    assert {"generated_mono", "tags"} <= set(columns)
    # A hostile table/column/definition never reaches SQLite.
    for args in (("entries; DROP TABLE entries;--", "x", "REAL"),
                 ("entries", "x; DROP TABLE entries;--", "REAL"),
                 ("entries", "x", "REAL; DROP TABLE entries;--")):
        with pytest.raises(ValueError):
            cache._ensure_column(*args)


def test_database_raw_sql_binds_named_params(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    import asyncio

    from pynext import Database
    from pynext.database import raw_sql as db_raw_sql

    async def run():
        db = Database(f"sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}")
        await db.startup()
        try:
            async with db.session() as session:
                await session.execute(db_raw_sql(
                    "CREATE TABLE items (name TEXT)"))
                payload = "x' OR '1'='1"
                await session.execute(db_raw_sql(
                    "INSERT INTO items (name) VALUES (:name)", {"name": payload}))
                await session.commit()
                rows = (await session.execute(db_raw_sql(
                    "SELECT name FROM items WHERE name = :name", {"name": payload}))
                ).fetchall()
                assert rows == [(payload,)]
                assert (await session.execute(db_raw_sql(
                    "SELECT COUNT(*) FROM items"))).scalar() == 1
        finally:
            await db.shutdown()

    asyncio.run(run())


def test_database_raw_sql_rejects_positional_params():
    from pynext.database import raw_sql as db_raw_sql

    with pytest.raises(TypeError, match="must be a dict of named parameters"):
        db_raw_sql("SELECT ?", ("x",))
    with pytest.raises(TypeError, match="must be a string"):
        db_raw_sql(42)
