"""Server-side input validation layer (S-P1-2, ROADMAP §1.2).

Django Forms → Marshmallow pattern: a handler declares its accepted
input once, as a :class:`Schema` of :class:`Field` specs, and the
framework validates at the handler entry point — exactly once, before
the handler runs. Client-side validation is UX only; this layer is
authoritative.

What is validated:

* structure — the body must be an object, unknown fields are rejected
  (mass-assignment protection), nested objects follow a nested schema;
* type — ``str`` / ``int`` / ``float`` / ``bool`` / ``list`` / ``dict``
  / ``any``, with the query-string coercions a handler needs (``"42"``
  → ``int``, ``"true"`` → ``bool``) and nothing else;
* range — numeric ``gt`` / ``ge`` / ``lt`` / ``le``, string/list
  ``min_length`` / ``max_length``, ``pattern`` (full match) and
  ``choices``;
* CSRF token binding — a declared ``csrf_field`` is bound to the token
  the request already carries (session token, else the signed CSRF
  cookie) with a constant-time comparison (Django lesson 2: the token
  is verified with ``constant_time_compare``; Litestar lesson 3: the
  signature is pre-compiled and constraints are fixed at compile time).

Schemas are compiled at construction: patterns are compiled to
``re.Pattern`` objects and contradictory constraints (``gt`` together
with ``ge``, ``min_length > max_length``, …) raise immediately, so a
bad schema fails at import/startup time instead of per request.

Failure mode (FastAPI lesson 4, S-P1-34): a violation is one HTTP 400
whose body carries field names and reason codes only — never the
submitted values. Secret-shaped fields (passwords, tokens, API keys)
are never echoed, and neither is any other input value::

    {"error": "validation_failed", "fields": {"age": ["range"], "name": ["required"]}}

File uploads are out of scope here; they are validated by a separate
track. A form part that arrives as an :class:`pynext.forms.UploadFile`
therefore fails a plain field's type check instead of being accepted.
"""

import hmac
import math
import re
from collections.abc import Mapping

from .http import (
    DEFAULT_SECURITY,
    JSONResponse,
    BadRequest,
    _header_values,
    _signed_csrf_token,
    _submitted_csrf_secret,
)

__all__ = [
    "ValidationError",
    "Field",
    "Schema",
    "validate_input",
    "validation_error_response",
    "validate_handler",
    "bind_csrf_token",
    "validate_request",
    "validate_handler_input",
]

#: Reason codes ever reported for a field. Fixed strings only — the
#: submitted value is never part of an error, so nothing secret (or
#: attacker-controlled) can leak into a response or a log line.
REASON_REQUIRED = "required"
REASON_TYPE = "type"
REASON_RANGE = "range"
REASON_LENGTH = "length"
REASON_PATTERN = "pattern"
REASON_CHOICE = "choice"
REASON_UNKNOWN = "unknown"
REASON_STRUCTURE = "structure"
REASON_CSRF = "csrf"

#: Field kinds. ``"any"`` accepts any JSON value (including ``None``);
#: every other kind rejects ``None`` with :data:`REASON_TYPE`.
KINDS = frozenset({"str", "int", "float", "bool", "list", "dict", "any"})

#: Where a field may come from. ``"any"`` looks in body, then query,
#: then path params, in that order.
SOURCES = frozenset({"any", "body", "query", "params"})

#: Sentinel for "no default": a missing optional field without a default
#: is simply absent from the cleaned data.
_MISSING = object()

#: String spellings accepted for a ``bool`` field from query/form input.
_BOOL_WORDS = {"true": True, "false": False, "1": True, "0": False}

#: ``int`` fields accept a decimal string (query params are strings);
#: anything else must already be an ``int``. ``bool`` is an ``int``
#: subclass and is rejected explicitly.
_INT_RE = re.compile(r"[+-]?\d+")


class ValidationError(Exception):
    """A failed validation: field names mapped to reason codes.

    ``errors`` maps each offending field to a tuple of reason codes
    (see the ``REASON_*`` constants). It never contains a submitted
    value — only field names and fixed codes — so it is safe to render
    into a 400 response or a log line as-is. Nested fields use dotted
    names (``"address.zip"``).
    """

    def __init__(self, errors):
        if not isinstance(errors, Mapping) or not errors:
            raise ValueError("ValidationError requires a non-empty errors mapping")
        cleaned = {}
        for name, codes in errors.items():
            if not isinstance(name, str) or not name:
                raise ValueError("ValidationError field names must be non-empty strings")
            if isinstance(codes, str):
                codes = (codes,)
            codes = tuple(codes)
            if not codes or not all(isinstance(code, str) and code for code in codes):
                raise ValueError("ValidationError reason codes must be non-empty strings")
            cleaned[name] = codes
        super().__init__("validation failed")
        self.errors = cleaned

    @property
    def fields(self):
        """Offending field names, sorted, for the 400 response body."""
        return sorted(self.errors)


class Field:
    """One accepted input field: its type and its constraints.

    Constraints are fixed here and compiled by :class:`Schema` at
    construction, so a handler's contract cannot drift per request.
    """

    def __init__(self, name, kind="str", *, required=True, default=_MISSING,
                 gt=None, ge=None, lt=None, le=None,
                 min_length=None, max_length=None, pattern=None,
                 choices=None, source="any", strip=False,
                 schema=None, item=None):
        if not isinstance(name, str) or not name:
            raise ValueError("field name must be a non-empty string")
        if kind not in KINDS:
            raise ValueError(f"unknown field kind {kind!r}")
        if source not in SOURCES:
            raise ValueError(f"unknown field source {source!r}")
        if not isinstance(required, bool):
            raise TypeError("required must be a bool")
        if gt is not None and ge is not None:
            raise ValueError(f"field {name!r}: gt and ge are mutually exclusive")
        if lt is not None and le is not None:
            raise ValueError(f"field {name!r}: lt and le are mutually exclusive")
        for bound in (gt, ge, lt, le):
            if bound is not None and isinstance(bound, bool):
                raise ValueError(f"field {name!r}: numeric bounds must not be bool")
        if (gt is not None and lt is not None and not gt < lt) or \
                (gt is not None and le is not None and not gt < le) or \
                (ge is not None and lt is not None and not ge < lt) or \
                (ge is not None and le is not None and not ge <= le):
            raise ValueError(f"field {name!r}: lower bound must be below the upper bound")
        if kind in ("str", "list"):
            for bound in (min_length, max_length):
                if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool) or bound < 0):
                    raise ValueError(f"field {name!r}: length bounds must be non-negative ints")
            if min_length is not None and max_length is not None and min_length > max_length:
                raise ValueError(f"field {name!r}: min_length must not exceed max_length")
        elif min_length is not None or max_length is not None:
            raise ValueError(f"field {name!r}: length bounds only apply to str/list fields")
        if kind not in ("int", "float") and any(b is not None for b in (gt, ge, lt, le)):
            raise ValueError(f"field {name!r}: numeric bounds only apply to int/float fields")
        if pattern is not None and kind != "str":
            raise ValueError(f"field {name!r}: pattern only applies to str fields")
        if schema is not None and kind != "dict":
            raise ValueError(f"field {name!r}: a nested schema only applies to dict fields")
        if item is not None and kind != "list":
            raise ValueError(f"field {name!r}: an item spec only applies to list fields")
        if not isinstance(strip, bool):
            raise TypeError("strip must be a bool")
        if strip and kind != "str":
            raise ValueError(f"field {name!r}: strip only applies to str fields")
        if choices is not None:
            choices = tuple(choices)
            if not choices:
                raise ValueError(f"field {name!r}: choices must not be empty")
        self.name = name
        self.kind = kind
        self.required = required
        self.default = default
        self.gt = gt
        self.ge = ge
        self.lt = lt
        self.le = le
        self.min_length = min_length
        self.max_length = max_length
        self.pattern = pattern  # compiled by Schema
        self.choices = choices
        self.source = source
        self.strip = strip
        self.schema = schema
        self.item = item

    def _coerce(self, value):
        """Coerce ``value`` to this field's kind, or return ``_MISSING``.

        Returns ``_MISSING`` when the value cannot be this kind at all
        (the caller reports :data:`REASON_TYPE`); coercion is deliberately
        narrow — a string ``"42"`` becomes an int because query params
        are strings, but a number never becomes a string and a bool is
        never an int.
        """
        kind = self.kind
        if kind == "any":
            return value
        if value is None:
            return _MISSING
        if kind == "str":
            if not isinstance(value, str):
                return _MISSING
            return value.strip() if self.strip else value
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return _BOOL_WORDS.get(value.strip().lower(), _MISSING)
            return _MISSING
        if kind == "int":
            if isinstance(value, bool):
                return _MISSING
            if isinstance(value, int):
                return value
            if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
                try:
                    return int(value.strip(), 10)
                except ValueError:
                    return _MISSING
            return _MISSING
        if kind == "float":
            if isinstance(value, bool):
                return _MISSING
            if isinstance(value, (int, float)):
                number = float(value)
                return number if math.isfinite(number) else _MISSING
            if isinstance(value, str):
                try:
                    number = float(value.strip())
                except ValueError:
                    return _MISSING
                return number if math.isfinite(number) else _MISSING
            return _MISSING
        if kind == "list":
            return value if isinstance(value, list) else _MISSING
        if kind == "dict":
            return value if isinstance(value, dict) else _MISSING
        return _MISSING  # pragma: no cover - KINDS is closed

    def _check_constraints(self, value, errors, name):
        """Append reason codes for constraint violations on ``value``.

        ``name`` is the full dotted field name the codes are filed under.
        """
        if self.kind in ("int", "float"):
            if self.gt is not None and not value > self.gt:
                errors.setdefault(name, []).append(REASON_RANGE)
            if self.ge is not None and not value >= self.ge:
                errors.setdefault(name, []).append(REASON_RANGE)
            if self.lt is not None and not value < self.lt:
                errors.setdefault(name, []).append(REASON_RANGE)
            if self.le is not None and not value <= self.le:
                errors.setdefault(name, []).append(REASON_RANGE)
        if self.kind in ("str", "list"):
            if self.min_length is not None and len(value) < self.min_length:
                errors.setdefault(name, []).append(REASON_LENGTH)
            if self.max_length is not None and len(value) > self.max_length:
                errors.setdefault(name, []).append(REASON_LENGTH)
        if self.kind == "str" and self.pattern is not None:
            if self.pattern.fullmatch(value) is None:
                errors.setdefault(name, []).append(REASON_PATTERN)
        if self.choices is not None and value not in self.choices:
            errors.setdefault(name, []).append(REASON_CHOICE)


class Schema:
    """A handler's input contract, compiled once at construction.

    ``Schema(Field("name"), Field("age", "int", ge=0, le=150))`` fixes
    the accepted fields, their types and their constraints; patterns
    are compiled here, so a bad pattern fails at startup, not on the
    first request that trips it.
    """

    def __init__(self, *fields, allow_unknown=False, csrf_field=None):
        if not fields:
            raise ValueError("a Schema requires at least one Field")
        if not isinstance(allow_unknown, bool):
            raise TypeError("allow_unknown must be a bool")
        seen = set()
        compiled = []
        for field in fields:
            if not isinstance(field, Field):
                raise TypeError("Schema fields must be Field instances")
            if field.name in seen:
                raise ValueError(f"duplicate field {field.name!r} in Schema")
            seen.add(field.name)
            if field.pattern is not None:
                try:
                    field.pattern = re.compile(field.pattern)
                except re.error as exc:
                    raise ValueError(f"field {field.name!r}: invalid pattern: {exc}") from exc
            compiled.append(field)
        if csrf_field is not None:
            if not isinstance(csrf_field, str) or csrf_field not in seen:
                raise ValueError("csrf_field must name a declared field")
            target = next(f for f in compiled if f.name == csrf_field)
            if target.kind != "str":
                raise ValueError("csrf_field must name a str field")
        self.fields = tuple(compiled)
        self.by_name = {field.name: field for field in compiled}
        self.allow_unknown = allow_unknown
        self.csrf_field = csrf_field

    @property
    def field_names(self):
        """Declared field names, in declaration order."""
        return [field.name for field in self.fields]

    def _validate_field(self, field, value, cleaned, errors, prefix):
        """Validate one field value; store the cleaned value or errors."""
        name = prefix + field.name
        coerced = field._coerce(value)
        if coerced is _MISSING:
            errors.setdefault(name, []).append(REASON_TYPE)
            return
        field._check_constraints(coerced, errors, name)
        if field.kind == "dict" and field.schema is not None and name not in errors:
            try:
                cleaned[name] = field.schema.validate(coerced)
            except ValidationError as exc:
                for sub, codes in exc.errors.items():
                    errors[f"{name}.{sub}"] = list(codes)
                return
        elif field.kind == "list" and field.item is not None and name not in errors:
            items = []
            failed = False
            for index, element in enumerate(coerced):
                item_name = f"{name}[{index}]"
                item_value = field.item._coerce(element)
                if item_value is _MISSING:
                    errors.setdefault(item_name, []).append(REASON_TYPE)
                    failed = True
                    continue
                before = len(errors.get(item_name, ()))
                field.item._check_constraints(item_value, errors, item_name)
                if len(errors.get(item_name, ())) > before:
                    failed = True
                    continue
                items.append(item_value)
            if not failed:
                cleaned[name] = items
            return
        if name not in errors:
            cleaned[name] = coerced

    def validate(self, data):
        """Validate one merged mapping; return the cleaned data.

        Raises :class:`ValidationError` with field names and reason
        codes only — never submitted values.
        """
        if not isinstance(data, Mapping):
            raise ValidationError({"body": (REASON_STRUCTURE,)})
        errors = {}
        cleaned = {}
        for field in self.fields:
            if field.name not in data:
                if field.required:
                    errors.setdefault(field.name, []).append(REASON_REQUIRED)
                elif field.default is not _MISSING:
                    cleaned[field.name] = field.default
                continue
            self._validate_field(field, data[field.name], cleaned, errors, prefix="")
        if not self.allow_unknown:
            for key in data:
                if key not in self.by_name:
                    errors.setdefault(key, []).append(REASON_UNKNOWN)
        if errors:
            raise ValidationError({name: tuple(codes) for name, codes in errors.items()})
        return cleaned

    def validate_sources(self, *, body=None, query=None, params=None):
        """Validate per-source mappings; return the cleaned data.

        Each field is read from its declared source (``"any"`` reads
        body, then query, then params). Unknown keys are rejected in
        body and query only — path-param keys come from the route
        pattern, so there is nothing attacker-controlled to reject
        there. ``params`` values are already typed by the router.
        """
        sources = {"body": body or {}, "query": query or {}, "params": params or {}}
        for source, mapping in sources.items():
            if not isinstance(mapping, Mapping):
                raise ValidationError({source: (REASON_STRUCTURE,)})
        errors = {}
        cleaned = {}
        for field in self.fields:
            value = _MISSING
            if field.source == "any":
                for source in ("body", "query", "params"):
                    if field.name in sources[source]:
                        value = sources[source][field.name]
                        break
            elif field.name in sources[field.source]:
                value = sources[field.source][field.name]
            if value is _MISSING:
                if field.required:
                    errors.setdefault(field.name, []).append(REASON_REQUIRED)
                elif field.default is not _MISSING:
                    cleaned[field.name] = field.default
                continue
            self._validate_field(field, value, cleaned, errors, prefix="")
        if not self.allow_unknown:
            for source in ("body", "query"):
                declared = {f.name for f in self.fields if f.source in ("any", source)}
                for key in sources[source]:
                    if key not in declared:
                        errors.setdefault(key, []).append(REASON_UNKNOWN)
        if errors:
            raise ValidationError({name: tuple(codes) for name, codes in errors.items()})
        return cleaned


def validate_input(data, schema):
    """Validate ``data`` against ``schema``; return the cleaned data.

    Thin convenience wrapper around :meth:`Schema.validate` for callers
    that already hold a merged mapping.
    """
    return schema.validate(data)


def validation_error_response(error):
    """Render a :class:`ValidationError` as a masked HTTP 400.

    The body carries the fixed ``"validation_failed"`` code plus the
    offending field names and their reason codes — never a submitted
    value, so a secret mistyped into a field cannot come back in the
    response (FastAPI lesson 4 / S-P1-34).
    """
    if not isinstance(error, ValidationError):
        raise TypeError("validation_error_response requires a ValidationError")
    return JSONResponse(
        {"error": "validation_failed",
         "fields": {name: list(error.errors[name]) for name in error.fields}},
        status=400,
    )


def validate_handler(schema):
    """Attach a :class:`Schema` to a route handler.

    The dispatcher validates the request against the schema at the
    handler entry point — once, before the handler runs — and answers
    400 when it fails. Handlers without a schema are untouched. ::

        schema = Schema(Field("name", min_length=1, max_length=80),
                        Field("age", "int", ge=0, le=150))

        @validate_handler(schema)
        async def POST(request):
            data = request.state["validated_input"]
            ...
    """
    if not isinstance(schema, Schema):
        raise TypeError("validate_handler requires a Schema")
    def decorator(fn):
        fn.__pynext_validation__ = schema
        return fn
    return decorator


def _expected_csrf_token(request, *, config):
    """The token a submitted CSRF token must match, or ``None``.

    The session token wins when a session is loaded (it is the token
    the server issued for this session); otherwise the signed CSRF
    cookie is unsigned and used. ``None`` means the request carries no
    token to bind against, so binding fails closed.
    """
    session = None
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        session = state.get("session")
    if session is not None:
        token = getattr(session, "_csrf", None)
        if isinstance(token, str) and token:
            return token
    cookie = None
    for value in _header_values(request, "cookie"):
        for part in value.decode("ascii", "ignore").split(";"):
            part = part.strip()
            if part.startswith(config.csrf_cookie_name + "="):
                cookie = part[len(config.csrf_cookie_name) + 1:]
                break
        if cookie is not None:
            break
    if not cookie:
        return None
    try:
        return _signed_csrf_token(cookie, config=config)
    except (ValueError, TypeError, UnicodeEncodeError):
        return None


def bind_csrf_token(request, submitted, *, config=None):
    """True when ``submitted`` matches the request's CSRF token.

    Constant-time comparison (Django lesson 2). ``False`` when either
    side is missing or not a string — binding fails closed, and the
    submitted value is never logged or echoed. A masked token (the
    form :meth:`Session.csrf_token` issues) is unmasked first, the
    same as :func:`validate_csrf` does; a malformed masked value is
    rejected outright.
    """
    if config is None:
        config = DEFAULT_SECURITY
    if not isinstance(submitted, str) or not submitted:
        return False
    expected = _expected_csrf_token(request, config=config)
    if not isinstance(expected, str) or not expected:
        return False
    secret = _submitted_csrf_secret(submitted)
    if secret is None:
        return False
    try:
        return hmac.compare_digest(secret, expected)
    except (TypeError, ValueError):
        return False


async def validate_request(request, schema, *, params=None, config=None):
    """Validate a request against ``schema``; return the cleaned data.

    Sources are gathered in one place — path params, the query string,
    and a JSON or form body — and each field is read from its declared
    source (``"any"`` prefers body, then query, then params). A body is
    only parsed when the schema declares a body-readable field, so a
    GET handler with query-only fields never pays for (or fails on) a
    body parse. Malformed JSON/form/query input is a validation
    failure on ``"body"`` / ``"query"`` with code ``"structure"``.

    When the schema declares ``csrf_field``, the cleaned value of that
    field is bound to the request's CSRF token; a missing or mismatched
    token fails the field with code ``"csrf"``.
    """
    if config is None:
        config = DEFAULT_SECURITY
    wants_body = any(f.source in ("any", "body") for f in schema.fields)
    wants_query = any(f.source in ("any", "query") for f in schema.fields)
    body = {}
    if wants_body and request.body:
        content_types = _header_values(request, "content-type")
        content_type = (content_types[0].decode("ascii", "ignore").lower()
                        if content_types else "")
        try:
            if "application/json" in content_type:
                parsed = await request.json()
                if not isinstance(parsed, dict):
                    raise ValidationError({"body": (REASON_STRUCTURE,)})
                body = parsed
            elif ("application/x-www-form-urlencoded" in content_type
                    or "multipart/form-data" in content_type):
                form = await request.form()
                body = {key: values[0] for key, values in form.items() if values}
            else:
                raise ValidationError({"body": (REASON_STRUCTURE,)})
        except ValidationError:
            raise
        except BadRequest as exc:
            raise ValidationError({"body": (REASON_STRUCTURE,)}) from exc
    query = {}
    if wants_query:
        try:
            query = {key: values[0] for key, values in request.query.items() if values}
        except BadRequest as exc:
            raise ValidationError({"query": (REASON_STRUCTURE,)}) from exc
    cleaned = schema.validate_sources(
        body=body, query=query,
        params=params if params is not None else getattr(request, "params", {}),
    )
    if schema.csrf_field is not None:
        submitted = cleaned.get(schema.csrf_field)
        if isinstance(submitted, str) and not bind_csrf_token(request, submitted, config=config):
            raise ValidationError({schema.csrf_field: (REASON_CSRF,)})
    return cleaned


async def validate_handler_input(request, handler, params, *, config=None):
    """Validate one handler call at its entry point.

    Returns ``None`` when the handler carries no schema (untouched) or
    validation passed — the cleaned data is stored on
    ``request.state["validated_input"]`` for the handler. Returns a
    masked 400 response when validation fails, so the dispatcher can
    answer without ever invoking the handler.
    """
    schema = getattr(handler, "__pynext_validation__", None)
    if schema is None:
        return None
    if config is None:
        config = DEFAULT_SECURITY
    try:
        cleaned = await validate_request(request, schema, params=params, config=config)
    except ValidationError as exc:
        return validation_error_response(exc)
    request.state["validated_input"] = cleaned
    return None
