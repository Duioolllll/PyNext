"""Minimal custom routes: redirects, rewrites and headers.

This is the small form of Next.js ``resolve-routes.ts``
(``redirect -> rewrite.beforeFiles -> check_fs -> afterFiles ->
fallback``): sources use the same ``[slug]`` / ``[...rest]`` /
``[[...rest]]`` segment grammar as file routes, destinations
interpolate those parameters as ``[name]`` placeholders, and the
dispatch order in :mod:`pynext.app` follows Next.js. ``has`` /
``missing`` conditions (header/cookie/query matching) are not
supported yet, and rewrites are internal-only — a rewrite
destination must start with ``/``.

Configuration (passed as ``custom_routes`` to :func:`pynext.create_app`)::

    {
        "redirects": [
            {"source": "/old/[slug]", "destination": "/blog/[slug]",
             "permanent": True},
        ],
        "rewrites": [
            {"source": "/api/[slug]", "destination": "/internal/[slug]"},
        ],
        # or, with explicit phases:
        # "rewrites": {"beforeFiles": [...], "afterFiles": [...],
        #              "fallback": [...]},
        "headers": [
            {"source": "/blog/[slug]",
             "headers": [{"key": "Cache-Control",
                          "value": "public, max-age=3600"}]},
        ],
    }

Redirects answer 308 when ``permanent`` is true, 307 otherwise, and
preserve the request query string when the destination has none.
A rewrite serves the destination path as if it had been requested
(beforeFiles before the route/public-asset check, afterFiles and
fallback only when that check misses). At most one rewrite applies
per request, so rewrite chains cannot loop. Header rules apply to
every response whose path matches, including redirects and 404s.

Everything is compiled and validated at startup: a bad source
pattern, a destination naming an unknown parameter, or a malformed
header fails ``create_app()`` instead of failing per request.
"""
import re
from urllib.parse import quote, urlsplit

from . import _native
from .routing import _segment_param

MAX_RULES_PER_LIST = 256
MAX_HEADERS_PER_RULE = 32
_REWRITE_PHASES = ("beforeFiles", "afterFiles", "fallback")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
_PLACEHOLDER = re.compile(r"\[([A-Za-z0-9_-]+)\]")
_RESERVED_PREFIXES = ("/_pynext",)


def _is_reserved(path):
    return path in ("/healthz", "/readyz") or path == "/_pynext" or path.startswith("/_pynext/")


def _pattern_params(pattern):
    if not isinstance(pattern, str) or not pattern.startswith("/"):
        raise ValueError(f"Custom route source must start with '/': {pattern!r}")
    if _is_reserved(pattern):
        raise ValueError(f"Custom route source is a reserved path: {pattern!r}")
    params = {}
    for segment in pattern.strip("/").split("/") if pattern != "/" else []:
        parsed = _segment_param(segment)
        if parsed:
            params[parsed[0]] = parsed[1]
    return params


def _check_destination(destination, source_params, *, allow_absolute):
    if not isinstance(destination, str) or not destination:
        raise ValueError(f"Custom route destination must be a nonempty string: {destination!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in destination):
        raise ValueError(f"Custom route destination must not contain control characters: {destination!r}")
    try:
        destination.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Custom route destination must be ASCII (percent-encode non-ASCII): {destination!r}") from exc
    if destination.startswith("/"):
        path = urlsplit(destination).path
        if _is_reserved(path):
            raise ValueError(f"Custom route destination is a reserved path: {destination!r}")
        if any(part in (".", "..") for part in path.split("/")):
            raise ValueError(f"Custom route destination must not contain '.' or '..': {destination!r}")
    elif allow_absolute and re.match(r"https?://", destination):
        parts = urlsplit(destination)
        if not parts.netloc:
            raise ValueError(f"Custom route destination needs a host: {destination!r}")
        if any(part in (".", "..") for part in parts.path.split("/")):
            raise ValueError(f"Custom route destination must not contain '.' or '..': {destination!r}")
    else:
        raise ValueError(
            "Custom route destination must start with '/'"
            + (" or be an absolute http(s) URL" if allow_absolute else "")
            + f": {destination!r}")
    for name in _PLACEHOLDER.findall(destination):
        if name not in source_params:
            raise ValueError(f"Custom route destination names unknown parameter [{name}]")
    if re.search(r"\[\[?\.\.\.", destination):
        raise ValueError(
            "Custom route destination interpolates parameters as [name], not "
            f"[...name] or [[...name]]: {destination!r}")


def _interpolate(destination, raw_params):
    """Replace ``[name]`` placeholders with encoded matched values."""
    def replace(match):
        values = raw_params[match.group(1)]
        return "/".join(quote(part, safe="") for part in values)
    return _PLACEHOLDER.sub(replace, destination)


def _check_headers(headers, *, where):
    if not isinstance(headers, list) or not headers or len(headers) > MAX_HEADERS_PER_RULE:
        raise ValueError(f"{where} must be a nonempty list of at most {MAX_HEADERS_PER_RULE} headers")
    seen = set()
    checked = []
    for entry in headers:
        if not isinstance(entry, dict) or set(entry) != {"key", "value"}:
            raise ValueError(f"{where} entries must be {{'key': ..., 'value': ...}} objects")
        key, value = entry["key"], entry["value"]
        if not isinstance(key, str) or _TOKEN.fullmatch(key) is None:
            raise ValueError(f"Invalid header name in {where}: {key!r}")
        if key.lower() in seen:
            raise ValueError(f"Duplicate header {key!r} in {where}")
        seen.add(key.lower())
        if (not isinstance(value, str) or
                any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value)):
            raise ValueError(f"Invalid header value for {key!r} in {where}")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(f"Header value for {key!r} in {where} must be Latin-1") from exc
        checked.append((key, value))
    return checked


class _RuleSet:
    """One compiled source->rule list, matched through the Rust router."""

    def __init__(self, rules):
        self._router = _native.Router([rule["source"] for rule in rules]) if rules else None
        self._rules = {rule["source"]: rule for rule in rules}

    def match(self, path):
        if self._router is None:
            return None
        matched = self._router.matches(path)
        if matched is None:
            return None
        # Router.matches returns (pattern, params, kinds): the third
        # element names each parameter's kind (dynamic / catch_all /
        # optional) and is not needed for interpolation.
        pattern, raw_params, _kinds = matched
        return self._rules[pattern], raw_params


class CustomRoutes:
    """Compiled redirects / rewrites / headers. Empty when no rules."""

    def __init__(self, redirects, rewrites, headers):
        self._redirects = _RuleSet(redirects)
        self._rewrites = {phase: _RuleSet(rewrites.get(phase, ())) for phase in _REWRITE_PHASES}
        self._headers = _RuleSet(headers)

    def __bool__(self):
        return bool(self._redirects._rules or self._headers._rules or
                    any(rules._rules for rules in self._rewrites.values()))

    def match_redirect(self, path):
        """Return (status, location) for the first matching redirect, else None."""
        found = self._redirects.match(path)
        if found is None:
            return None
        rule, raw_params = found
        location = _interpolate(rule["destination"], raw_params)
        return 308 if rule["permanent"] else 307, location

    def match_rewrite(self, path, phase):
        """Return the rewritten path for ``phase``, else None."""
        found = self._rewrites[phase].match(path)
        if found is None:
            return None
        rule, raw_params = found
        return _interpolate(rule["destination"], raw_params)

    def headers_for(self, path):
        """Merged (key, value) headers for every rule matching ``path``.

        All matching rules apply in order; a later rule replaces an
        earlier value for the same header name.
        """
        # A source pattern only matches one rule, but the same path can
        # match at most one rule per set, so this is a single lookup;
        # the merge loop keeps the multi-rule behaviour explicit.
        merged = {}
        found = self._headers.match(path)
        if found is not None:
            rule, _ = found
            for key, value in rule["headers"]:
                merged[key.lower()] = (key, value)
        return list(merged.values())

    def apply_to_response(self, response, path):
        """Apply matching header rules to ``response`` (replace-or-append)."""
        for key, value in self.headers_for(path):
            # Response headers are normalised to lowercase everywhere
            # else in pynext (see http.Response.__init__); keep that
            # invariant so lookups by lowercase name keep working.
            encoded_key = key.lower().encode("ascii")
            encoded_value = value.encode("latin-1")
            response.headers = [(k, v) for k, v in response.headers
                                if k.lower() != encoded_key.lower()]
            response.headers.append((encoded_key, encoded_value))


def _compile_redirects(rules, *, where):
    if not isinstance(rules, list) or len(rules) > MAX_RULES_PER_LIST:
        raise ValueError(f"{where} must be a list of at most {MAX_RULES_PER_LIST} rules")
    compiled = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) - {"source", "destination", "permanent"}:
            raise ValueError(f"{where}[{index}] must have 'source' and 'destination' "
                             f"(optional 'permanent')")
        if "source" not in rule or "destination" not in rule:
            raise ValueError(f"{where}[{index}] must have 'source' and 'destination'")
        source_params = _pattern_params(rule["source"])
        _check_destination(rule["destination"], source_params, allow_absolute=True)
        permanent = rule.get("permanent", False)
        if not isinstance(permanent, bool):
            raise ValueError(f"{where}[{index}] 'permanent' must be a bool")
        compiled.append({"source": rule["source"], "destination": rule["destination"],
                         "permanent": permanent})
    return compiled


def _compile_rewrites(rewrites):
    if isinstance(rewrites, list):
        rewrites = {"beforeFiles": rewrites}
    if not isinstance(rewrites, dict) or set(rewrites) - set(_REWRITE_PHASES):
        raise ValueError("rewrites must be a list (beforeFiles) or an object with "
                         "beforeFiles/afterFiles/fallback lists")
    compiled = {}
    for phase in _REWRITE_PHASES:
        rules = rewrites.get(phase, [])
        if not isinstance(rules, list) or len(rules) > MAX_RULES_PER_LIST:
            raise ValueError(f"rewrites.{phase} must be a list of at most "
                             f"{MAX_RULES_PER_LIST} rules")
        phase_rules = []
        for index, rule in enumerate(rules):
            where = f"rewrites.{phase}[{index}]"
            if not isinstance(rule, dict) or set(rule) != {"source", "destination"}:
                raise ValueError(f"{where} must have exactly 'source' and 'destination'")
            source_params = _pattern_params(rule["source"])
            _check_destination(rule["destination"], source_params, allow_absolute=False)
            phase_rules.append({"source": rule["source"], "destination": rule["destination"]})
        compiled[phase] = phase_rules
    return compiled


def _compile_headers(rules):
    if not isinstance(rules, list) or len(rules) > MAX_RULES_PER_LIST:
        raise ValueError(f"headers must be a list of at most {MAX_RULES_PER_LIST} rules")
    compiled = []
    for index, rule in enumerate(rules):
        where = f"headers[{index}]"
        if not isinstance(rule, dict) or set(rule) != {"source", "headers"}:
            raise ValueError(f"{where} must have exactly 'source' and 'headers'")
        _pattern_params(rule["source"])
        compiled.append({"source": rule["source"],
                         "headers": _check_headers(rule["headers"], where=where)})
    return compiled


def compile_custom_routes(config):
    """Compile a ``custom_routes`` config dict, or return empty rules.

    ``None`` means no custom routes. Anything malformed raises
    ``ValueError`` so the app fails at startup, not per request.
    """
    if config is None:
        return CustomRoutes([], {}, [])
    if not isinstance(config, dict) or set(config) - {"redirects", "rewrites", "headers"}:
        raise ValueError("custom_routes must be an object with optional "
                         "'redirects', 'rewrites' and 'headers' lists")
    redirects = _compile_redirects(config.get("redirects", []), where="redirects")
    rewrites = _compile_rewrites(config.get("rewrites", []))
    headers = _compile_headers(config.get("headers", []))
    return CustomRoutes(redirects, rewrites, headers)
