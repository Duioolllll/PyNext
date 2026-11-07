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

#[derive(Debug, Default)]
struct Node {
    endpoint: Option<String>,
    static_children: BTreeMap<String, Node>,
    dynamic: Option<(String, Box<Node>)>,
    catch_all: Option<(String, Box<Node>)>,
    optional_catch_all: Option<(String, Box<Node>)>,
}

fn parameter(segment: &str) -> Result<Option<(ParamKind, &str)>, String> {
    if !segment.starts_with('[') && !segment.ends_with(']') {
        return Ok(None);
    }
    if !segment.starts_with('[') || !segment.ends_with(']') {
        return Err(format!("Malformed route segment: {segment}"));
    }
    let inner = &segment[1..segment.len() - 1];
    let (optional, inner) = match inner.strip_prefix('[') {
        Some(rest) => {
            let Some(rest) = rest.strip_suffix(']') else {
                return Err(format!(
                    "Optional or malformed route parameters are not supported: {segment}"
                ));
            };
            (true, rest)
        }
        None => (false, inner),
    };
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
    if name.is_empty()
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        || !name.chars().any(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return Err(format!("Invalid parameter name: {segment}"));
    }
    Ok(Some((kind, name)))
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
            if let Some((kind, name)) = parameter(segment)? {
                if kind != ParamKind::Dynamic && i + 1 != segments.len() {
                    return Err("Catch-all must be the last part of the URL".into());
                }
                if matches!(kind, ParamKind::CatchAll) && node.optional_catch_all.is_some()
                    || matches!(kind, ParamKind::OptionalCatchAll) && node.catch_all.is_some()
                {
                    return Err(
                        "Cannot combine a catch-all and an optional catch-all at the same level"
                            .into(),
                    );
                }
                let comparable_name = comparable(name);
                if !names.insert(comparable_name) {
                    return Err(format!("Repeated or ambiguous parameter name: {name}"));
                }
                let slot = match kind {
                    ParamKind::Dynamic => &mut node.dynamic,
                    ParamKind::CatchAll => &mut node.catch_all,
                    ParamKind::OptionalCatchAll => &mut node.optional_catch_all,
                };
                let (existing, child) =
                    slot.get_or_insert_with(|| (name.to_owned(), Box::default()));
                if comparable(existing) != comparable(name) {
                    return Err(format!(
                        "Different slug names for the same dynamic path: {existing}, {name}"
                    ));
                }
                node = child;
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

    pub fn matches(&self, path: &str) -> Option<(String, HashMap<String, Vec<String>>)> {
        let path_parts = parts(path).ok()?;
        let mut params = HashMap::new();
        self.match_node(&self.root, &path_parts, 0, &mut params)
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
    ) -> Option<(String, HashMap<String, Vec<String>>)> {
        if index == path_parts.len() {
            if let Some(route) = &node.endpoint {
                return Some((route.clone(), params.clone()));
            }
            // An optional catch-all matches zero segments: the route below
            // it is reachable with an empty parameter list.
            if let Some((name, child)) = &node.optional_catch_all {
                params.insert(name.clone(), vec![]);
                let hit = self.match_node(child, path_parts, index, params);
                if hit.is_some() {
                    return hit;
                }
                params.remove(name);
            }
            return None;
        }
        let segment = path_parts[index];
        if let Some(child) = node.static_children.get(segment) {
            if let Some(hit) = self.match_node(child, path_parts, index + 1, params) {
                return Some(hit);
            }
        }
        if let Some((name, child)) = &node.dynamic {
            params.insert(name.clone(), vec![segment.to_string()]);
            let hit = self.match_node(child, path_parts, index + 1, params);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
        }
        if let Some((name, child)) = &node.catch_all {
            params.insert(
                name.clone(),
                path_parts[index..].iter().map(|s| s.to_string()).collect(),
            );
            let hit = self.match_node(child, path_parts, path_parts.len(), params);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
        }
        if let Some((name, child)) = &node.optional_catch_all {
            params.insert(
                name.clone(),
                path_parts[index..].iter().map(|s| s.to_string()).collect(),
            );
            let hit = self.match_node(child, path_parts, path_parts.len(), params);
            if hit.is_some() {
                return hit;
            }
            params.remove(name);
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
        prop::sample::select(vec!["a", "b", "blog", "posts", "x1", "new", "about"])
    }

    fn segment_strategy() -> impl Strategy<Value = String> {
        prop_oneof![
            static_segment().prop_map(str::to_owned),
            prop::sample::select(vec!["[id]", "[slug]"]).prop_map(str::to_owned),
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
            } else if let Some(name) = segment.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
                rebuilt.push(params[name][0].clone());
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
            if let Some((pattern, params)) = first {
                let rebuilt = rebuild_path(&pattern, &params);
                prop_assert_eq!(&rebuilt, &path);
                prop_assert_eq!(router.matches(&rebuilt), Some((pattern, params)));
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
    fn rejects_conflicts_and_invalid_patterns() {
        for paths in [
            vec!["/blog/[id]", "/blog/[slug]"],
            vec!["/[...a]/x"],
            vec!["/[id]/[id]"],
            vec!["/[[id]]"],
            vec!["/[..id]"],
            vec!["/[...a]", "/[[...b]]"],
            vec!["/[[...a]]/x"],
        ] {
            assert!(
                Router::new(paths.iter().map(|s| s.to_string()).collect()).is_err(),
                "{paths:?}"
            );
        }
    }
}
