"""Catch-all normalization for SSG path enumeration.

Reduced port of Next.js ``server/lib/router-utils/normalize-catchall-routes.ts``
(``normalizeCatchAllRoutes``), adapted to PyNext's file-based router.

Next.js attaches each catch-all route to the app paths it can match, then —
in strict mode — prunes routes whose parallel-route slots cannot all render
and reports the pages left unmatched. PyNext has no parallel routes (``@slot``
directories are not slots) and no interception routes, so the pruning half
has no PyNext equivalent and is deliberately not imported (see REPORT.md
section 4: parallel routes are out of scope). What this module keeps is the
normalization core plus strict mode's question, restated for PyNext: is every
enumerated static path accounted for by exactly one most-specific owner?

Concretely, at build time (``build.write_manifest``) this module:

- reports, for each enumerated static path, which catch-all routes would
  also match it — the static page wins at runtime, so the report makes the
  shadowing visible instead of silent (``normalize_catch_all_routes``);
- reports which catch-all routes cover no enumerated path at all, i.e. they
  serve only paths ``generate_static_params`` did not enumerate
  (``dynamic_only``);
- verifies the enumeration is complete: every enumerated path must match at
  least one route, and the most-specific matching route — computed here
  independently of the Rust trie, from the segment specificity ordering of
  Next.js ``sortable-routes.ts`` (static 0, dynamic 1, catch-all 2,
  optional catch-all 3, shorter wins on a tie) — must be the path's owning
  pattern (``verify_static_enumeration``). A disagreement means the
  enumeration and the router disagree about who owns a path, so the build
  fails instead of rendering the page under the wrong route.

Only page routes take part: API routes (``route.py``) do not participate in
static enumeration, and the router's own self-check in ``_render_static_pages``
remains the authority for page-vs-API priority.
"""
import re
from dataclasses import dataclass, field

__all__ = ["CatchAllReport", "normalize_catch_all_routes", "verify_static_enumeration"]

# Same segment grammar as build._segment_param: `[name]`, `[...name]`,
# `[[...name]]`. Kept in sync with it; this module stays dependency-free so
# the normalization can be tested without building an app.
_SEGMENT_RE = re.compile(r"\[(\[)?(\.\.\.)?([A-Za-z0-9_-]+)\](\])?")

# Segment specificity, mirroring Next.js sortable-routes.ts
# getSegmentSpecificity(): static 0, dynamic 1, catch-all 2, optional
# catch-all 3.
_SPECIFICITY = {"static": 0, "dynamic": 1, "catch_all": 2, "optional": 3}


def _parse_segment(segment):
    """Parse one route segment.

    Returns (name, kind) where kind is "dynamic", "catch_all" or
    "optional", or None for a static segment. Raises ValueError on a
    malformed segment, mirroring build._segment_param.
    """
    if not segment.startswith("["):
        return None
    match = _SEGMENT_RE.fullmatch(segment)
    if match is None:
        raise ValueError(f"Invalid route segment: {segment}")
    double_open, dots, name, double_close = match.groups()
    if bool(double_open) != bool(double_close):
        raise ValueError(f"Invalid route segment: {segment}")
    if double_open and not dots:
        raise ValueError(f"Optional catch-all must use [[...name]]: {segment}")
    kind = "optional" if double_open else ("catch_all" if dots else "dynamic")
    return name, kind


def _split(pattern):
    if not isinstance(pattern, str) or not pattern.startswith("/"):
        raise ValueError(f"Invalid route pattern: {pattern!r}")
    return pattern.strip("/").split("/") if pattern != "/" else []


def _segment_kinds(pattern):
    """Return the parsed segments of a route pattern.

    Each entry is ("static", segment) or (kind, name). A catch-all or
    optional catch-all anywhere but the last segment is invalid — the
    router only allows it last — so it fails here too.
    """
    parsed = []
    segments = _split(pattern)
    for index, segment in enumerate(segments):
        result = _parse_segment(segment)
        if result is None:
            parsed.append(("static", segment))
        else:
            name, kind = result
            if kind in {"catch_all", "optional"} and index != len(segments) - 1:
                raise ValueError(f"Catch-all must be the last part of the URL: {pattern}")
            parsed.append((kind, name))
    return parsed


def _specificity(parsed):
    """Specificity tuple for one pattern, for most-specific-first ordering."""
    return tuple(_SPECIFICITY[kind] for kind, _ in parsed)


def _is_catch_all(parsed):
    return bool(parsed) and parsed[-1][0] in {"catch_all", "optional"}


def _matches(parsed, path_segments):
    """Whether a concrete path matches a parsed pattern, segment by segment.

    A dynamic segment consumes exactly one segment; a catch-all consumes
    one or more remaining segments; an optional catch-all consumes zero or
    more. Static segments must match literally.
    """
    index = 0
    for kind, value in parsed:
        if kind == "static":
            if index >= len(path_segments) or path_segments[index] != value:
                return False
            index += 1
        elif kind == "dynamic":
            if index >= len(path_segments):
                return False
            index += 1
        elif kind == "catch_all":
            # Last segment by construction; needs at least one more.
            return index < len(path_segments)
        elif kind == "optional":
            # Last segment by construction; matches zero or more.
            return True
        else:  # pragma: no cover - _parse_segment only yields these kinds.
            raise AssertionError(f"Unknown segment kind: {kind}")
    return index == len(path_segments)


def _matching_patterns(parsed_patterns, path):
    """All patterns matching a concrete path, most specific first.

    Ordering follows Next.js sortable-routes.ts: segment specificity
    (static < dynamic < catch-all < optional catch-all), compared segment
    by segment, with the shorter pattern winning when one is a prefix of
    the other. Returns a list of (pattern, parsed) pairs.
    """
    segments = _split(path)
    matched = [(pattern, parsed) for pattern, parsed in parsed_patterns
               if _matches(parsed, segments)]
    matched.sort(key=lambda item: (_specificity(item[1]), len(item[1]), item[0]))
    return matched


@dataclass(frozen=True)
class CatchAllReport:
    """Build-time catch-all coverage of the enumerated static paths.

    ``coverage`` maps each catch-all pattern to the enumerated static paths
    (owned by other, more specific patterns) it would also serve if those
    pages did not exist. ``shadowed`` is the same relation from the path's
    side: each enumerated path mapped to the catch-all patterns that would
    also match it, most specific first — the static page wins at runtime.
    ``dynamic_only`` lists catch-all patterns covering no enumerated path at
    all: they serve only paths ``generate_static_params`` did not enumerate.
    ``catch_all_pages`` maps each catch-all pattern to the paths it
    enumerated itself.
    """
    coverage: dict = field(default_factory=dict)
    shadowed: dict = field(default_factory=dict)
    dynamic_only: list = field(default_factory=list)
    catch_all_pages: dict = field(default_factory=dict)

    def to_json(self):
        """Deterministic JSON form for the build manifest."""
        return {
            "coverage": {pattern: sorted(paths) for pattern, paths in sorted(self.coverage.items())},
            "shadowed": {path: list(patterns) for path, patterns in sorted(self.shadowed.items())},
            "dynamic_only": sorted(self.dynamic_only),
            "catch_all_pages": {pattern: sorted(paths)
                                for pattern, paths in sorted(self.catch_all_pages.items())},
        }


def normalize_catch_all_routes(patterns, static_paths):
    """Normalize catch-all routes against the enumerated static paths.

    ``patterns`` is an iterable of page route patterns; ``static_paths``
    maps each enumerated static path to its owning pattern (as produced by
    ``build._render_static_pages``, which today only enumerates SSG pages —
    ISR pages cannot export ``generate_static_params`` yet, so they do not
    enumerate). Catch-all patterns are considered most specific first
    (segment specificity, then shorter pattern, mirroring the ordering in
    ``_matching_patterns``), so the lists in the report are in runtime
    priority order.

    A required catch-all ``/docs/[...path]`` matches paths strictly below
    its base (``/docs/a``, ``/docs/a/b``) but not the base itself; an
    optional catch-all ``/docs/[[...path]]`` also matches the base
    (``/docs``). A catch-all never shadows its own enumerated paths.
    """
    parsed_patterns = [(pattern, _segment_kinds(pattern)) for pattern in patterns]
    by_pattern = dict(parsed_patterns)
    catch_alls = [(pattern, parsed) for pattern, parsed in parsed_patterns
                  if _is_catch_all(parsed)]
    # Most specific first: segment specificity, then shorter pattern —
    # the same ordering _matching_patterns uses, so the report lists
    # covering catch-alls in runtime priority order.
    catch_alls.sort(key=lambda item: (_specificity(item[1]), len(item[1]), item[0]))

    coverage = {}
    shadowed = {}
    catch_all_pages = {}
    for path, owner in static_paths.items():
        path_segments = _split(path)
        covering = [pattern for pattern, parsed in catch_alls
                    if pattern != owner and _matches(parsed, path_segments)]
        if covering:
            shadowed[path] = covering
            for pattern in covering:
                coverage.setdefault(pattern, []).append(path)
        if owner in by_pattern and _is_catch_all(by_pattern[owner]):
            catch_all_pages.setdefault(owner, []).append(path)
    covered = set(coverage) | set(catch_all_pages)
    dynamic_only = sorted(pattern for pattern, _ in catch_alls if pattern not in covered)
    return CatchAllReport(coverage=coverage, shadowed=shadowed,
                          dynamic_only=dynamic_only, catch_all_pages=catch_all_pages)


def verify_static_enumeration(patterns, static_paths):
    """Check the SSG/ISR enumeration is complete (the adapted slot check).

    Next.js strict mode reports pages no retained matcher claims and routes
    that cannot fill every slot of a layout level. PyNext has a single
    ``children`` slot per layout level and no parallel routes, so the
    adapted check is: every enumerated static path must be claimed by at
    least one pattern, and the most-specific matching pattern — computed
    here independently of the Rust trie — must be the path's owning
    pattern. Raises ValueError naming the path otherwise.
    """
    parsed_patterns = [(pattern, _segment_kinds(pattern)) for pattern in patterns]
    by_pattern = dict(parsed_patterns)
    for path, owner in static_paths.items():
        if owner not in by_pattern:
            raise ValueError(f"Static path {path} is owned by unknown route {owner}")
        matched = _matching_patterns(parsed_patterns, path)
        if not matched:
            raise ValueError(f"Static path {path} is not matched by any route")
        winner = matched[0][0]
        if winner != owner:
            raise ValueError(
                f"Static path {path} is owned by {owner} but the most specific "
                f"matching route is {winner}")
        if len(matched) > 1 and _specificity(matched[0][1]) == _specificity(matched[1][1]):
            raise ValueError(
                f"Static path {path} is ambiguously matched by {matched[0][0]} "
                f"and {matched[1][0]}")
