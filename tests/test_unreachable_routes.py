"""Unreachable-route build warning (REPORT.md §2.3 P2, tracks/a-routing.md §2.10).

A route is unreachable only when *every* path it could match is matched
and won by another route. Partial shadowing is normal -- a catch-all
still serves the lengths no more specific route covers -- and warns
nothing.
"""
import random

import pytest

from pynext.build import find_unreachable_routes, write_manifest


def test_no_warning_for_normal_routes():
    assert find_unreachable_routes([
        "/", "/posts", "/posts/new", "/posts/[id]", "/[root]", "/[...rest]",
    ]) == {}


def test_partial_shadowing_is_not_reported():
    # The report's own example: the catch-all loses its one-segment
    # paths to [slug] but still serves longer ones.
    assert find_unreachable_routes(["/docs/[...path]", "/docs/[slug]"]) == {}
    # Same for an optional catch-all: /docs/a/b is still only its own.
    assert find_unreachable_routes(["/docs", "/docs/[[...path]]", "/docs/[slug]"]) == {}


def test_catch_all_fully_shadowed():
    assert find_unreachable_routes([
        "/docs/[...path]", "/docs/[slug]", "/docs/[slug]/[...rest]",
    ]) == {"/docs/[...path]": ["/docs/[slug]", "/docs/[slug]/[...rest]"]}


def test_root_catch_all_fully_shadowed():
    assert find_unreachable_routes(["/[...rest]", "/[id]", "/[id]/[...x]"]) == {
        "/[...rest]": ["/[id]", "/[id]/[...x]"]
    }


def test_optional_catch_all_shadowed_by_static_and_dynamic():
    assert find_unreachable_routes([
        "/docs", "/docs/[[...path]]", "/docs/[slug]", "/docs/[slug]/[...rest]",
    ]) == {"/docs/[[...path]]": ["/docs", "/docs/[slug]", "/docs/[slug]/[...rest]"]}


def test_fixed_routes_are_never_unreachable():
    assert find_unreachable_routes(["/about", "/[id]"]) == {}
    assert find_unreachable_routes(["/a/b", "/a/[x]", "/a/[...rest]"]) == {}
    assert find_unreachable_routes(["/docs", "/docs/[slug]", "/docs/[slug]/[sub]"]) == {}


def test_tail_without_unbounded_stealer_stays_reachable():
    # Only fixed-length stealers: the catch-all keeps its longer paths.
    assert find_unreachable_routes([
        "/docs/[...path]", "/docs/[slug]", "/docs/[slug]/[sub]",
    ]) == {}


def test_deep_prefix_and_multiple_unreachable():
    assert find_unreachable_routes([
        "/a/b/[...rest]", "/a/b/[id]", "/a/b/[id]/[...x]",
        "/c/[...rest]", "/c/[id]", "/c/[id]/[...x]",
    ]) == {
        "/a/b/[...rest]": ["/a/b/[id]", "/a/b/[id]/[...x]"],
        "/c/[...rest]": ["/c/[id]", "/c/[id]/[...x]"],
    }


def test_accepts_manifest_routes_dict():
    assert find_unreachable_routes({
        "/docs/[...path]": {"kind": "page"},
        "/docs/[slug]": {"kind": "page"},
        "/docs/[slug]/[...rest]": {"kind": "page"},
    }) == {"/docs/[...path]": ["/docs/[slug]", "/docs/[slug]/[...rest]"]}


def test_invalid_pattern_raises():
    with pytest.raises(ValueError):
        find_unreachable_routes(["/docs/[...path]/extra"])
    with pytest.raises(ValueError):
        find_unreachable_routes(["/docs/[bad name]"])


def _write_page(path, body="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from pynext import page, h\n"
        "@page()\n"
        f"def Page(params): return h('p', {{}}, {body!r})\n"
    )


def test_build_warns_on_unreachable_route(tmp_path, capsys):
    app = tmp_path / "app"
    _write_page(app / "docs" / "[...path]" / "page.py")
    _write_page(app / "docs" / "[slug]" / "page.py")
    _write_page(app / "docs" / "[slug]" / "[...rest]" / "page.py")
    write_manifest(tmp_path)
    err = capsys.readouterr().err
    assert "warning: unreachable route /docs/[...path] is shadowed by" in err
    assert "/docs/[slug]" in err and "/docs/[slug]/[...rest]" in err


def test_build_is_quiet_without_unreachable_routes(tmp_path, capsys):
    app = tmp_path / "app"
    _write_page(app / "docs" / "[...path]" / "page.py")
    _write_page(app / "docs" / "[slug]" / "page.py")
    write_manifest(tmp_path)
    assert "unreachable route" not in capsys.readouterr().err


# Cross-check against the real Rust trie: for a random route set, a route
# is unreachable exactly when no fresh-token witness path of any length
# matches it first. Fresh tokens never collide with a static segment, so
# if any witness exists, a fresh one exists; lengths past the longest
# route are all equivalent, so probing up to two past that bound covers
# the tail regime.
def test_matches_router_on_random_route_sets():
    from pynext import _native

    rng = random.Random(20261110)
    statics = ["a", "b", "docs", "blog"]
    for _ in range(300):
        routes = set()
        for _ in range(rng.randint(1, 5)):
            depth = rng.randint(1, 3)
            segments = []
            for pos in range(depth):
                roll = rng.random()
                if roll < 0.5:
                    segments.append(rng.choice(statics))
                elif roll < 0.8:
                    segments.append(f"[p{pos}]")
                else:
                    break
            tail = rng.random()
            if tail < 0.25:
                segments.append("[...rest]")
            elif tail < 0.4:
                segments.append("[[...opt]]")
            if segments:
                routes.add("/" + "/".join(segments))
        routes = sorted(routes)
        if not routes:
            continue
        try:
            router = _native.Router(routes)
        except Exception:
            continue  # conflicting set (e.g. different slug names): skip
        max_len = max(len(r.strip("/").split("/")) for r in routes)
        found = find_unreachable_routes(routes)
        for route in routes:
            parsed_min = len(route.strip("/").split("/"))
            # min length: catch-all tail needs one segment, optional none.
            if route.endswith("]]"):
                min_len = parsed_min - 1
            elif route.endswith("]") and "[..." in route.rsplit("/", 1)[-1]:
                min_len = parsed_min
            else:
                min_len = parsed_min
            witnessed = False
            for length in range(min_len, max_len + 3):
                parts = []
                for segment in route.strip("/").split("/"):
                    if segment.startswith("[[..."):
                        parts.extend(["__probe__"] * max(0, length - len(parts)))
                        break
                    if segment.startswith("[..."):
                        parts.extend(["__probe__"] * max(1, length - len(parts)))
                        break
                    if segment.startswith("["):
                        parts.append("__probe__")
                    else:
                        parts.append(segment)
                if len(parts) != length:
                    continue
                path = "/" + "/".join(parts) if parts else "/"
                match = router.matches(path)
                if match is not None and match[0] == route:
                    witnessed = True
                    break
            if route in found:
                assert not witnessed, f"{route} reported unreachable but {path} matches it first"
            else:
                assert witnessed, f"{route} not reported but no witness found in {routes}"
