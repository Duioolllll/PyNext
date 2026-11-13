"""Route sorting by specificity, translated from Next.js ``sortable-routes.ts``.

Next.js keeps two route orderings: the deprecated ``sorted-routes.ts`` trie
(``UrlNode`` + ``_smoosh``) and the newer comparator in
``shared/lib/router/utils/sortable-routes.ts`` (``getSegmentSpecificity``,
``compareRouteSegments``, ``sortPages`` / ``sortSortableRoutes``). PyNext's
router already implements the trie ordering in Rust
(``crates/pynext-router/src/lib.rs``, ``ordered()``); this module is the
comparator side of the same rule, so the ordering can be explained, reused
and tested without going through the trie.

Ordering rule (most specific first), per route segment left to right:

1. segment specificity: static (0) < affixed dynamic ``v[version]`` (1) <
   dynamic ``[id]`` (2) < catch-all ``[...rest]`` (3) < optional catch-all
   ``[[...rest]]`` (4);
2. same specificity: lexicographic order;
3. one route a prefix of the other: the shorter route first.

So ``/posts`` < ``/posts/new`` < ``/posts/[id]`` < ``/posts/[...rest]``.

Two deliberate deviations from the TypeScript original:

* Lexicographic comparison uses plain code-point order (``<`` on ``str``),
  not ``String.localeCompare``. ``localeCompare`` is locale-dependent and can
  order case and punctuation differently; code-point order is what the Rust
  trie uses (``BTreeMap<String>``), and the property test in
  ``tests/test_sortable_routes.py`` pins that this comparator and
  ``Router.ordered_routes()`` agree on every valid route set.
* ``compare_routes`` returns exactly ``-1``, ``0`` or ``1``. The original
  returns the raw specificity difference and the ``localeCompare`` result,
  whose magnitudes callers must not rely on; only the sign is meaningful.
* The specificity tiers add an affixed-dynamic tier (1) between static (0)
  and plain dynamic (2). Next.js has no affixed segments, but PyNext does
  (``v[version]``, ``[name].json``), and the Rust trie ranks them there
  (``static > affixed > dynamic``). Without the tier, an affixed segment
  fell through to static (0) and sorted ahead of a static segment it
  cannot beat at runtime (A1-M4; same tier as
  ``pynext.catchall._SPECIFICITY``).

Source: Next.js ``packages/next/src/shared/lib/router/utils/sortable-routes.ts``
at commit ``0cf37690`` (the revision the Next.js/PyNext comparison report
used); see ``third_party/NEXTJS.md`` for the MIT notice. PyNext has no i18n
rendered-page distinction, so a plain route pattern is its own source page;
``compare_sortable_routes`` / ``sort_sortable_routes`` keep the two-tier
(source, then page) comparison for callers that do distinguish them.
"""

from functools import cmp_to_key
import re

__all__ = [
    "get_segment_specificity",
    "compare_routes",
    "sort_routes",
    "compare_sortable_routes",
    "sort_sortable_routes",
]

# An affixed dynamic parameter: a well-formed [name] with a static prefix
# and/or suffix (v[version], [name].json, api[version]). Same segment
# grammar as build._segment_param / catchall._SEGMENT_RE; catch-all
# parameters cannot carry affixes, so "..." never appears here.
_AFFIXED_PARAM = re.compile(r"[^[\]]*\[[A-Za-z0-9_-]+\][^[\]]*")


def get_segment_specificity(segment):
    """Specificity of one route segment: 0 static, 1 affixed dynamic,
    2 dynamic, 3 catch-all, 4 optional catch-all.

    Deliberately departs from Next.js ``getSegmentSpecificity`` (which
    has no affixed tier): PyNext supports affixed dynamic parameters
    (``v[version]``, ``[name].json``), and the Rust trie ranks them
    between static and plain dynamic (A1-M4; same tier as
    ``pynext.catchall._SPECIFICITY``). A segment containing ``[``
    without forming a well-formed parameter (for example ``[id``) is
    still static (0), and a malformed optional catch-all missing its
    closing brackets (``[[...slug]``) still falls through to dynamic (2).
    """
    if "[" not in segment:
        return 0
    if segment.startswith("[[...") and segment.endswith("]]"):
        return 4
    if segment.startswith("[...") and segment.endswith("]"):
        return 3
    if segment.startswith("[") and segment.endswith("]"):
        return 2
    if _AFFIXED_PARAM.fullmatch(segment) is not None:
        return 1
    return 0


def _segments(path):
    # Next.js: path.split('/').filter(Boolean) — leading, trailing and
    # doubled slashes contribute no segment.
    return [part for part in path.split("/") if part]


def compare_routes(a, b):
    """Compare two route patterns by specificity; -1 if ``a`` first, 1 if ``b`` first, 0 if equal.

    Mirrors Next.js ``compareRouteSegments``: segment by segment, lower
    specificity number wins, then lexicographic order, and a route that runs
    out of segments first (the shorter route) wins at that position.
    """
    segments_a = _segments(a)
    segments_b = _segments(b)
    for index in range(max(len(segments_a), len(segments_b))):
        seg_a = segments_a[index] if index < len(segments_a) else ""
        seg_b = segments_b[index] if index < len(segments_b) else ""
        if not seg_a and seg_b:
            return -1
        if seg_a and not seg_b:
            return 1
        if not seg_a and not seg_b:
            return 0
        specificity_a = get_segment_specificity(seg_a)
        specificity_b = get_segment_specificity(seg_b)
        if specificity_a != specificity_b:
            return -1 if specificity_a < specificity_b else 1
        if seg_a != seg_b:
            return -1 if seg_a < seg_b else 1
    return 0


def sort_routes(routes):
    """Sort route patterns by specificity (``sortPages``). Returns a new list; the input is untouched."""
    return sorted(routes, key=cmp_to_key(compare_routes))


def compare_sortable_routes(a, b):
    """Compare ``(source_page, page)`` pairs: source specificity first, page as tiebreaker.

    Mirrors Next.js ``compareSortableRoutes``. In PyNext the source page is
    the route pattern itself (there is no rendered-page variant), so callers
    that only have patterns should use ``compare_routes`` instead.
    """
    source_result = compare_routes(a[0], b[0])
    if source_result != 0:
        return source_result
    return compare_routes(a[1], b[1])


def sort_sortable_routes(routes):
    """Sort ``(source_page, page)`` pairs (``sortSortableRoutes``). Returns a new list."""
    return sorted(routes, key=cmp_to_key(compare_sortable_routes))
