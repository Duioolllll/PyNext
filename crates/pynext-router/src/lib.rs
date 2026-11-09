//! Route ordering and conflict detection adapted from Next.js `sorted-routes.ts`.
//! See third_party/NEXTJS.md for the pinned revision and MIT notice.

use std::collections::{BTreeMap, HashMap, HashSet};

/// Parameter names compare after stripping `-` and `_`: `[my-id]` and
/// `[my_id]` name the same slot, so they must not be reported as different
/// slug names.
fn comparable(name: &str) -> String {
    name.chars().filter(|c| c.is_ascii_alphanumeric()).collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ParamKind {
    Dynamic,
    CatchAll,
    OptionalCatchAll,
}

impl ParamKind {
    fn as_str(&self) -> &'static str {
        match self {
            ParamKind::Dynamic => "dynamic",
            ParamKind::CatchAll => "catch_all",
            ParamKind::OptionalCatchAll => "optional",
        }
    }
}

/// A matched route: the route pattern, each parameter's matched segments,
/// and each parameter's kind (`"dynamic"`, `"catch_all"` or `"optional"`,
/// the same strings `python/pynext/build.py` `_segment_param` uses).
/// The kind travels with the match so callers never re-derive it from
/// the pattern string: a substring test cannot tell the kinds apart
/// (`"[...a]"` is a substring of `"/[a]/[...a2]"`).
pub type RouteMatch = (
    String,
    HashMap<String, Vec<String>>,
    HashMap<String, &'static str>,
);

#[derive(Debug, Default)]
struct Node {
    endpoint: Option<String>,
    static_children: BTreeMap<String, Node>,
    dynamic: Option<(String, Box<Node>)>,
    catch_all: Option<(String, Box<Node>)>,
    optional_catch_all: Option<(String, Box<Node>)>,
    /// Dynamic segments with a static prefix and/or suffix (`v[version]`,
    /// `[name].json`), keyed by `(prefix, suffix)`; the value is the
    /// parameter name and the child node. They match only when the request
    /// segment starts with the prefix, ends with the suffix and has a
    /// non-empty middle, and they win over a bare `[name]` at the same
    /// level because their literal parts make them more specific.
    affixed: BTreeMap<(String, String), (String, Box<Node>)>,
}

/// One dynamic segment of a route pattern.
///
/// A plain `[name]` has an empty prefix and suffix; Next.js
/// `PARAMETER_PATTERN` (`get-dynamic-param.ts`) also allows a static
/// prefix and/or suffix around the parameter, as in `v[version]` or
/// `[name].json`. Catch-all parameters cannot carry a prefix or suffix.
struct SegmentParam {
    kind: ParamKind,
    name: String,
    prefix: String,
    suffix: String,
}

fn parameter(segment: &str) -> Result<Option<SegmentParam>, String> {
    let Some(open) = segment.find('[') else {
        if segment.contains(']') {
            return Err(format!("Malformed route segment: {segment}"));
        }
        return Ok(None);
    };
    let prefix = &segment[..open];
    if prefix.contains(']') {
        return Err(format!("Malformed route segment: {segment}"));
    }
    let rest = &segment[open..];
    // An optional parameter opens with `[[` and closes with `]]`.
    let (optional, inner, suffix) = if let Some(after) = rest.strip_prefix("[[") {
        let Some(end) = after.find("]]") else {
            return Err(format!(
                "Optional or malformed route parameters are not supported: {segment}"
            ));
        };
        (true, &after[..end], &after[end + 2..])
    } else {
        let Some(end) = rest[1..].find(']') else {
            return Err(format!("Malformed route segment: {segment}"));
        };
        (false, &rest[1..1 + end], &rest[1 + end + 1..])
    };
    if suffix.contains(['[', ']']) {
        return Err(format!("Malformed route segment: {segment}"));
    }
    let (kind, name) = match inner.strip_prefix("...") {
        Some(name) => (
            if optional {
                ParamKind::OptionalCatchAll
            } else {
                ParamKind::CatchAll
            },
            name,
        ),
        None => {
            if optional {
                return Err(format!(
                    "Optional catch-all must use [[...name]]: {segment}"
                ));
            }
            (ParamKind::Dynamic, inner)
        }
    };
    if kind != ParamKind::Dynamic && (!prefix.is_empty() || !suffix.is_empty()) {
        return Err(format!(
            "Catch-all parameters cannot have a prefix or suffix: {segment}"
        ));
    }
    if name.is_empty()
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        || !name.chars().any(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return Err(format!("Invalid parameter name: {segment}"));
    }
    Ok(Some(SegmentParam {
        kind,
        name: name.to_owned(),
        prefix: prefix.to_owned(),
        suffix: suffix.to_owned(),
    }))
}

fn parts(path: &str) -> Result<Vec<&str>, String> {
    if !path.starts_with('/') || (path.len() > 1 && path.ends_with('/')) || path.contains("//") {
        return Err(format!("Route must be an absolute normalized path: {path}"));
    }
    if path == "/" {
        return Ok(vec![]);
    }
    let parts: Vec<&str> = path[1..].split('/').collect();
    if parts
        .iter()
        .any(|p| *p == "." || *p == ".." || p.contains(['?', '#', '\\', '\0']))
    {
        return Err(format!("Invalid route path: {path}"));
    }
    Ok(parts)
}

impl Node {
    fn insert(&mut self, route: &str) -> Result<(), String> {
        let segments = parts(route)?;
        let mut node = self;
        let mut names = HashSet::new();
        for (i, segment) in segments.iter().enumerate() {
            if let Some(param) = parameter(segment)? {
                if param.kind != ParamKind::Dynamic && i + 1 != segments.len() {
                    return Err("Catch-all must be the last part of the URL".into());
                }
                if matches!(param.kind, ParamKind::CatchAll) && node.optional_catch_all.is_some()
                    || matches!(param.kind, ParamKind::OptionalCatchAll) && node.catch_all.is_some()
                {
                    return Err(
                        "Cannot combine a catch-all and an optional catch-all at the same level"
                            .into(),
                    );
                }
                let comparable_name = comparable(&param.name);
                if !names.insert(comparable_name) {
                    return Err(format!(
                        "Repeated or ambiguous parameter name: {}",
                        param.name
                    ));
                }
                if param.prefix.is_empty() && param.suffix.is_empty() {
                    let slot = match param.kind {
                        ParamKind::Dynamic => &mut node.dynamic,
                        ParamKind::CatchAll => &mut node.catch_all,
                        ParamKind::OptionalCatchAll => &mut node.optional_catch_all,
                    };
                    let (existing, child) =
                        slot.get_or_insert_with(|| (param.name.clone(), Box::default()));
                    if comparable(existing) != comparable(&param.name) {
                        return Err(format!(
                            "Different slug names for the same dynamic path: {existing}, {}",
                            param.name
                        ));
                    }
                    node = child;
                } else {
                    // A prefix/suffix parameter is its own slot, keyed by
                    // its literal parts: `/files/[name].json` and
                    // `/files/[name].txt` coexist, but two routes sharing
                    // the same literals must agree on the parameter name.
                    let key = (param.prefix.clone(), param.suffix.clone());
                    let (existing, child) = node
                        .affixed
                        .entry(key)
                        .or_insert_with(|| (param.name.clone(), Box::default()));
                    if comparable(existing) != comparable(&param.name) {
                        return Err(format!(
                            "Different slug names for the same dynamic path: {existing}, {}",
                            param.name
                        ));
                    }
                    node = child;
                }
            } else {
                if segment.contains(['[', ']']) {
                    return Err(format!("Malformed route segment: {segment}"));
                }
                node = node
                    .static_children
                    .entry((*segment).to_string())
                    .or_default();
            }
        }
        if node.endpoint.replace(route.to_owned()).is_some() {
            return Err(format!("Duplicate route: {route}"));
        }
        Ok(())
    }

    fn ordered(&self, out: &mut Vec<String>) {
        if let Some(route) = &self.endpoint {
            out.push(route.clone());
        }
        for child in self.static_children.values() {
            child.ordered(out);
        }
        // Affixed segments sort before a bare dynamic segment, matching
        // their higher match priority (static > affixed > dynamic).
        for (_, child) in self.affixed.values() {
            child.ordered(out);
        }
        if let Some((_, child)) = &self.dynamic {
            child.ordered(out);
        }
        if let Some((_, child)) = &self.catch_all {
            child.ordered(out);
        }
        if let Some((_, child)) = &self.optional_catch_all {
            child.ordered(out);
        }
    }
}

#[derive(Debug)]
pub struct Router {
    root: Node,
    routes: Vec<String>,
}

impl Router {
    pub fn new(routes: Vec<String>) -> Result<Self, String> {
        let mut root = Node::default();
        for route in routes {
            root.insert(&route)?;
        }
        let mut routes = Vec::new();
        root.ordered(&mut routes);
        Ok(Self { root, routes })
    }
    pub fn routes(&self) -> &[String] {
        &self.routes
    }

    pub fn matches(&self, path: &str) -> Option<RouteMatch> {
        let path_parts = parts(path).ok()?;
        let mut params = HashMap::new();
        // Parameter kinds travel with the match instead of being re-derived
        // from the pattern string: the trie knows whether a slot is a
        // dynamic segment, a catch-all or an optional catch-all, and a
        // substring test on the pattern cannot tell them apart reliably
        // (for example "[...a]" is a substring of "/[a]/[...a2]").
        // Kind strings match python/pynext/build.py `_segment_param`.
        let mut kinds = HashMap::new();
        self.match_node(&self.root, &path_parts, 0, &mut params, &mut kinds)
    }

    // Walk the trie instead of re-parsing every route: static segments win over
    // dynamic ones, which win over catch-alls, and a dead static branch falls
    // back to the dynamic branch at the same level.
    fn match_node(
        &self,
        node: &Node,
        path_parts: &[&str],
        index: usize,
        params: &mut HashMap<String, Vec<String>>,
        kinds: &mut HashMap<String, &'static str>,
    ) -> Option<RouteMatch> {
        if index == path_parts.len() {
            if let Some(route) = &node.endpoint {
                return Some((route.clone(), params.clone(), kinds.clone()));
            }
            // An optional catch-all matches zero segments: the route below
            // it is reachable with an empty parameter list.
            if let Some((name, child)) = &node.optional_catch_all {
                params.insert(name.clone(), vec![]);
                kinds.insert(name.clone(), ParamKind::OptionalCatchAll.as_str());
                let hit = self.match_node(child, path_parts, index, params, kinds);
                if hit.is_some() {
                    return hit;
                }
                params.remove(name);
                kinds.remove(name);
            }
            return None;
        }
        let segment = path_parts[index];
        if let Some(child) = node.static_children.get(segment) {
            if let Some(hit) = self.match_node(child, path_parts, index + 1, params, kinds) {
                return Some(hit);
            }
        }
        // Affixed segments (`v[version]`, `[name].json`) are more specific
        // than a bare dynamic segment, so they are tried first. Several
        // affixed entries can match one segment, so a failed child
        // backtracks to the next candidate.
        for ((prefix, suffix), (name, child)) in &node.affixed {
            let Some(middle) = segment
                .strip_prefix(prefix.as_str())
                .and_then(|rest| rest.strip_suffix(suffix.as_str()))
                .filter(|middle| !middle.is_empty())
            else {
                continue;
            };
            params.insert(name.clone(), vec![middle.to_string()]);
            kinds.insert(name.clone(), ParamKind::Dynamic.as_str());
            let hit = self.match_node(child, path_parts, index + 1, params, kinds);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
            kinds.remove(name);
        }
        if let Some((name, child)) = &node.dynamic {
            params.insert(name.clone(), vec![segment.to_string()]);
            kinds.insert(name.clone(), ParamKind::Dynamic.as_str());
            let hit = self.match_node(child, path_parts, index + 1, params, kinds);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
            kinds.remove(name);
        }
        if let Some((name, child)) = &node.catch_all {
            params.insert(
                name.clone(),
                path_parts[index..].iter().map(|s| s.to_string()).collect(),
            );
            kinds.insert(name.clone(), ParamKind::CatchAll.as_str());
            let hit = self.match_node(child, path_parts, path_parts.len(), params, kinds);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
            kinds.remove(name);
        }
        if let Some((name, child)) = &node.optional_catch_all {
            params.insert(
                name.clone(),
                path_parts[index..].iter().map(|s| s.to_string()).collect(),
            );
            kinds.insert(name.clone(), ParamKind::OptionalCatchAll.as_str());
            let hit = self.match_node(child, path_parts, path_parts.len(), params, kinds);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
            kinds.remove(name);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn router(paths: &[&str]) -> Router {
        Router::new(paths.iter().map(|s| s.to_string()).collect()).unwrap()
    }

    fn static_segment() -> impl Strategy<Value = &'static str> {
        prop::sample::select(vec![
            "a",
            "b",
            "blog",
            "posts",
            "x1",
            "new",
            "about",
            "v2",
            "report.json",
        ])
    }

    fn segment_strategy() -> impl Strategy<Value = String> {
        prop_oneof![
            static_segment().prop_map(str::to_owned),
            prop::sample::select(vec![
                "[id]",
                "[slug]",
                "v[version]",
                "[name].json",
                "[name].txt"
            ])
            .prop_map(str::to_owned),
        ]
    }

    fn route_strategy() -> impl Strategy<Value = String> {
        (
            prop::collection::vec(segment_strategy(), 1..3),
            any::<bool>(),
        )
            .prop_map(|(mut segments, catch_all)| {
                if catch_all {
                    segments.push("[...rest]".to_owned());
                }
                format!("/{}", segments.join("/"))
            })
    }

    fn rebuild_path(pattern: &str, params: &HashMap<String, Vec<String>>) -> String {
        let mut rebuilt: Vec<String> = Vec::new();
        for segment in pattern.trim_start_matches('/').split('/') {
            if let Some(name) = segment
                .strip_prefix("[...")
                .and_then(|s| s.strip_suffix(']'))
            {
                rebuilt.extend(params[name].iter().cloned());
            } else if let Some(open) = segment.find('[') {
                // A plain `[name]` or an affixed `v[version]` / `[name].json`
                // segment rebuilds as prefix + value + suffix.
                let close = segment.find(']').unwrap();
                let name = &segment[open + 1..close];
                let mut out = String::new();
                out.push_str(&segment[..open]);
                out.push_str(&params[name][0]);
                out.push_str(&segment[close + 1..]);
                rebuilt.push(out);
            } else {
                rebuilt.push(segment.to_owned());
            }
        }
        format!("/{}", rebuilt.join("/"))
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(64))]
        #[test]
        fn random_route_sets_build_cleanly_and_match(
            routes in prop::collection::vec(route_strategy(), 1..6)
        ) {
            let mut unique = routes.clone();
            unique.sort();
            unique.dedup();
            match Router::new(unique.clone()) {
                Err(_) => {}
                Ok(router) => {
                    let mut ordered = router.routes().to_vec();
                    ordered.sort();
                    prop_assert_eq!(&ordered, &unique);
                    for route in &unique {
                        prop_assert!(router.matches(route).is_some());
                    }
                }
            }
        }
        #[test]
        fn random_matches_rebuild_their_path(
            routes in prop::collection::vec(route_strategy(), 1..6),
            path_segments in prop::collection::vec(static_segment(), 1..4),
        ) {
            let mut unique = routes.clone();
            unique.sort();
            unique.dedup();
            let Ok(router) = Router::new(unique) else {
                return Ok(());
            };
            let path = format!("/{}", path_segments.join("/"));
            let first = router.matches(&path);
            prop_assert_eq!(&router.matches(&path), &first);
            if let Some((pattern, params, _kinds)) = first {
                let rebuilt = rebuild_path(&pattern, &params);
                prop_assert_eq!(&rebuilt, &path);
                prop_assert_eq!(router.matches(&rebuilt), Some((pattern, params, _kinds)));
            }
        }
        #[test]
        fn matches_never_panics_on_arbitrary_paths(path in any::<String>()) {
            let router = router(&["/", "/posts/[id]", "/posts/new", "/[...rest]"]);
            let _ = router.matches(&path);
        }
    }
    #[test]
    fn source_guided_order_and_matching() {
        let r = router(&[
            "/posts/[id]",
            "/[...rest]",
            "/posts/new",
            "/",
            "/[root]",
            "/posts",
        ]);
        assert_eq!(
            r.routes(),
            &[
                "/",
                "/posts",
                "/posts/new",
                "/posts/[id]",
                "/[root]",
                "/[...rest]"
            ]
        );
        assert_eq!(r.matches("/posts/new").unwrap().0, "/posts/new");
        assert_eq!(r.matches("/posts/42").unwrap().1["id"], vec!["42"]);
        assert_eq!(r.matches("/a/b").unwrap().1["rest"], vec!["a", "b"]);
    }
    #[test]
    fn trie_matching_prefers_static_and_backtracks() {
        let r = router(&["/a/b/c", "/a/[x]/d", "/a/b"]);
        assert_eq!(r.matches("/a/b/d").unwrap().0, "/a/[x]/d");
        assert_eq!(r.matches("/a/b/d").unwrap().1["x"], vec!["b"]);
        assert_eq!(r.matches("/a/b/c").unwrap().0, "/a/b/c");
        assert_eq!(r.matches("/a/b").unwrap().0, "/a/b");
        assert!(r.matches("/a/b/c/d").is_none());
    }
    #[test]
    fn catch_all_requires_at_least_one_segment() {
        let r = router(&["/[...rest]"]);
        assert!(r.matches("/").is_none());
        assert_eq!(r.matches("/a").unwrap().1["rest"], vec!["a"]);
    }
    #[test]
    fn hyphen_and_underscore_param_names_share_one_slot() {
        // `[my-id]` and `[my_id]` are the same slot: they must not fail with a
        // "different slug names" error. Two routes of the identical shape are
        // still one route too many, so the pair fails as a duplicate instead.
        let err = Router::new(vec!["/[my-id]".to_string(), "/[my_id]".to_string()]).unwrap_err();
        assert!(err.contains("Duplicate route"), "{err}");
        // Genuinely different names still conflict.
        assert!(Router::new(vec!["/blog/[id]".to_string(), "/blog/[slug]".to_string()]).is_err());
        // Variants at the same position with different continuations coexist,
        // keeping the first-seen spelling as the parameter name.
        let r = router(&["/a/[my-id]/b", "/a/[my_id]/c"]);
        assert_eq!(r.matches("/a/x/b").unwrap().1["my-id"], vec!["x"]);
        assert_eq!(r.matches("/a/x/c").unwrap().1["my-id"], vec!["x"]);
    }
    #[test]
    fn optional_catch_all_matches_zero_or_more_segments() {
        let r = router(&["/docs/[[...path]]"]);
        assert_eq!(r.matches("/docs").unwrap().1["path"], Vec::<String>::new());
        assert_eq!(r.matches("/docs/a").unwrap().1["path"], vec!["a"]);
        assert_eq!(r.matches("/docs/a/b").unwrap().1["path"], vec!["a", "b"]);
        // A static route at the same prefix wins over the optional catch-all,
        // and a dynamic segment wins over it for exactly one segment.
        let r = router(&["/docs", "/docs/[[...path]]", "/docs/[slug]"]);
        assert_eq!(r.matches("/docs").unwrap().0, "/docs");
        assert_eq!(r.matches("/docs/a").unwrap().0, "/docs/[slug]");
        assert_eq!(r.matches("/docs/a/b").unwrap().0, "/docs/[[...path]]");
        assert_eq!(r.routes(), &["/docs", "/docs/[slug]", "/docs/[[...path]]"]);
        // Optional catch-all is the lowest priority overall.
        let r = router(&["/[[...rest]]", "/[root]", "/about"]);
        assert_eq!(r.matches("/about").unwrap().0, "/about");
        assert_eq!(r.matches("/x").unwrap().0, "/[root]");
        assert_eq!(r.matches("/").unwrap().0, "/[[...rest]]");
        assert_eq!(r.matches("/").unwrap().1["rest"], Vec::<String>::new());
        assert_eq!(r.matches("/a/b").unwrap().1["rest"], vec!["a", "b"]);
    }
    #[test]
    fn matches_reports_parameter_kinds() {
        let r = router(&["/posts/[id]", "/docs/[...path]", "/files/[[...rest]]"]);
        let (_, _, kinds) = r.matches("/posts/42").unwrap();
        assert_eq!(kinds["id"], "dynamic");
        let (_, _, kinds) = r.matches("/docs/a/b").unwrap();
        assert_eq!(kinds["path"], "catch_all");
        let (_, _, kinds) = r.matches("/files").unwrap();
        assert_eq!(kinds["rest"], "optional");
        // A catch-all name must not leak into a plain dynamic parameter
        // whose name is its prefix: "[...a]" is a substring of
        // "/[a]/[...a2]", so a string test on the pattern misclassifies it.
        let r = router(&["/[a]/[...a2]"]);
        let (_, params, kinds) = r.matches("/x/y/z").unwrap();
        assert_eq!(kinds["a"], "dynamic");
        assert_eq!(kinds["a2"], "catch_all");
        assert_eq!(params["a"], vec!["x"]);
        assert_eq!(params["a2"], vec!["y", "z"]);
    }
    #[test]
    fn prefix_and_suffix_parameters_match() {
        let r = router(&["/files/[name].json", "/v[version]/api", "/files/[name].txt"]);
        assert_eq!(
            r.matches("/files/report.json").unwrap().0,
            "/files/[name].json"
        );
        assert_eq!(
            r.matches("/files/report.json").unwrap().1["name"],
            vec!["report"]
        );
        assert_eq!(
            r.matches("/files/report.json").unwrap().2["name"],
            "dynamic"
        );
        assert_eq!(r.matches("/files/data.txt").unwrap().0, "/files/[name].txt");
        assert_eq!(r.matches("/v2/api").unwrap().1["version"], vec!["2"]);
        // An empty middle never matches: `/files/.json` has no name.
        assert!(r.matches("/files/.json").is_none());
        assert!(r.matches("/v/api").is_none());
        // A bare dynamic segment does not swallow the suffix.
        let r = router(&["/files/[name].json", "/files/[name]"]);
        assert_eq!(
            r.matches("/files/report.json").unwrap().0,
            "/files/[name].json"
        );
        assert_eq!(r.matches("/files/report").unwrap().0, "/files/[name]");
        // Affixed segments outrank a bare dynamic segment at the same level.
        let r = router(&["/v[version]/api", "/[root]/api"]);
        assert_eq!(r.matches("/v2/api").unwrap().0, "/v[version]/api");
        // A static segment still wins over an affixed one.
        let r = router(&["/files/[name].json", "/files/latest.json"]);
        assert_eq!(
            r.matches("/files/latest.json").unwrap().0,
            "/files/latest.json"
        );
        // Affixed routes sort before bare dynamic routes.
        let r = router(&["/[root]", "/v[version]/api", "/files/[name].json"]);
        assert_eq!(
            r.routes(),
            &["/files/[name].json", "/v[version]/api", "/[root]"]
        );
    }
    #[test]
    fn rejects_conflicts_and_invalid_patterns() {
        for paths in [
            vec!["/blog/[id]", "/blog/[slug]"],
            vec!["/[...a]/x"],
            vec!["/[id]/[id]"],
            vec!["/[[id]]"],
            vec!["/[..id]"],
            vec!["/[...a]", "/[[...b]]"],
            vec!["/[[...a]]/x"],
            vec!["/files/[...rest].json"],
            vec!["/v[...version]"],
            vec!["/files/[[...rest]].json"],
            vec!["/files/[a].json", "/files/[b].json"],
            vec!["/[a][b]"],
        ] {
            assert!(
                Router::new(paths.iter().map(|s| s.to_string()).collect()).is_err(),
                "{paths:?}"
            );
        }
    }
}
