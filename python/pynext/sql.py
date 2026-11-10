"""Explicit escape hatches for SQL (S-P1-10).

Default rule: SQL is built with placeholders and values are passed
separately — never interpolated into the statement string. The only
sanctioned way to write raw SQL is :func:`raw_sql`, whose name makes
the risk visible at the call site (Django's ``RawSQL``/``.extra()``
principle: the dangerous path must look dangerous).

Two backends, one rule:

* DB-API / ``sqlite3`` (the ISR cache, session store internals):
  :func:`raw_sql` returns a :class:`RawSQL` carrying the statement and
  its bound parameters separately; :meth:`RawSQL.execute` passes them
  to the connection untouched.
* SQLAlchemy (application code through :class:`pynext.database.Database`):
  :func:`pynext.database.raw_sql` wraps ``sqlalchemy.text()`` and binds
  named parameters — same rule, SQLAlchemy spelling.

Identifiers (table/column names) can never be bound parameters, so DDL
that must name them goes through :func:`quote_identifier`, which only
accepts ``[A-Za-z_][A-Za-z0-9_]*`` and double-quotes the result, and
:func:`validate_ddl_definition`, which only accepts a fixed shape of
column definition. Anything else raises :class:`ValueError` instead of
reaching the database.
"""

import re

__all__ = ["RawSQL", "raw_sql", "quote_identifier", "validate_ddl_definition"]

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# A column definition is a type, an optional NOT NULL, and an optional
# DEFAULT with a literal (quoted string, number, or ``[]`` JSON default).
# No expressions, no function calls, no subqueries — DDL fragments are
# framework constants, never user input.
_DDL_DEFINITION = re.compile(
    r"(?:REAL|TEXT|INTEGER|BLOB)"
    r"(?: NOT NULL)?"
    r"(?: DEFAULT (?:'[^']*'|[0-9]+(?:\.[0-9]+)?|\[\]))?"
    r"\Z"
)


class RawSQL:
    """A raw SQL statement with its parameters kept separate.

    Construct only through :func:`raw_sql`. ``sql`` is the statement
    text with ``?`` placeholders; ``params`` are the bound values.
    Values are never interpolated into ``sql`` — that is the whole
    point of this type.
    """

    __slots__ = ("sql", "params")

    def __init__(self, sql, params=()):
        if not isinstance(sql, str):
            raise TypeError("raw SQL statement must be a string")
        if params is None:
            params = ()
        if not isinstance(params, (tuple, list, dict)):
            raise TypeError("raw SQL parameters must be a tuple, list, dict, or None")
        self.sql = sql
        self.params = tuple(params) if isinstance(params, (list, tuple)) else dict(params)

    def execute(self, connection):
        """Run the statement on a DB-API connection, params bound separately."""
        return connection.execute(self.sql, self.params)

    def __repr__(self):  # pragma: no cover - debugging aid only
        return f"RawSQL({self.sql!r}, params=<{len(self.params)} bound>)"


def raw_sql(sql, params=None):
    """Explicit escape hatch for raw SQL (DB-API / ``sqlite3`` path).

    Use only when placeholders cannot express the statement (DDL, or a
    construct the query builder has no spelling for). Values MUST be
    passed through ``params`` — never f-stringed, ``%``-formatted, or
    concatenated into ``sql``. Call sites using this function are the
    review-checklist item "escape-hatch usages" (see
    ``docs/SECURITY.md``).
    """
    return RawSQL(sql, () if params is None else params)


def quote_identifier(name):
    """Quote a SQL identifier (table/column name) after strict validation.

    Only ``[A-Za-z_][A-Za-z0-9_]*`` is accepted; anything else raises
    :class:`ValueError`. The result is double-quoted, so even a
    validated name cannot break out of identifier position.
    """
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def validate_ddl_definition(definition):
    """Validate a DDL column definition fragment; return it unchanged.

    Only a bare type (``REAL``/``TEXT``/``INTEGER``/``BLOB``) with an
    optional ``NOT NULL`` and an optional ``DEFAULT`` literal is
    accepted. Anything else raises :class:`ValueError`.
    """
    if not isinstance(definition, str) or not _DDL_DEFINITION.fullmatch(definition):
        raise ValueError(f"Invalid DDL column definition: {definition!r}")
    return definition
