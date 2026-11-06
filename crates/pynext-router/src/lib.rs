//! Route ordering and conflict detection adapted from Next.js `sorted-routes.ts`.
//! See third_party/NEXTJS.md for the pinned revision and MIT notice.

use std::collections::{BTreeMap, HashMap, HashSet};

#[derive(Default)]
struct Node {
    endpoint: Option<String>,
    static_children: BTreeMap<String, Node>,
    dynamic: Option<(String, Box<Node>)>,
    catch_all: Option<(String, Box<Node>)>,
}

fn parameter(segment: &str) -> Result<Option<(bool, &str)>, String> {
    if !segment.starts_with('[') && !segment.ends_with(']') {
        return Ok(None);
    }
    if !segment.starts_with('[') || !segment.ends_with(']') {
        return Err(format!("Malformed route segment: {segment}"));
    }
    let inner = &segment[1..segment.len() - 1];
    if inner.starts_with('[') || inner.ends_with(']') {
        return Err(format!(
            "Optional or malformed route parameters are not supported: {segment}"
        ));
    }
    let (catch_all, name) = match inner.strip_prefix("...") {
        Some(name) => (true, name),
        None => (false, inner),
    };
    if name.is_empty()
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        || !name.chars().any(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return Err(format!("Invalid parameter name: {segment}"));
    }
    Ok(Some((catch_all, name)))
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
            if let Some((catch_all, name)) = parameter(segment)? {
                if catch_all && i + 1 != segments.len() {
                    return Err("Catch-all must be the last part of the URL".into());
                }
                let comparable: String = name
                    .chars()
                    .filter(|c| c.is_ascii_alphanumeric() || *c == '_')
                    .collect();
                if !names.insert(comparable) {
                    return Err(format!("Repeated or ambiguous parameter name: {name}"));
                }
                let slot = if catch_all {
                    &mut node.catch_all
                } else {
                    &mut node.dynamic
                };
                let (existing, child) =
                    slot.get_or_insert_with(|| (name.to_owned(), Box::default()));
                if existing != name {
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
    }
}

#[derive(Debug)]
pub struct Router {
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
        Ok(Self { routes })
    }
    pub fn routes(&self) -> &[String] {
        &self.routes
    }

    pub fn matches(&self, path: &str) -> Option<(String, HashMap<String, Vec<String>>)> {
        let path_parts = parts(path).ok()?;
        for route in &self.routes {
            let route_parts = parts(route).ok()?;
            let mut params = HashMap::new();
            let mut i = 0;
            let mut matches = true;
            for seg in route_parts {
                if i >= path_parts.len() {
                    matches = false;
                    break;
                }
                match parameter(seg).ok()? {
                    Some((true, name)) => {
                        params.insert(
                            name.to_string(),
                            path_parts[i..].iter().map(|s| (*s).to_string()).collect(),
                        );
                        i = path_parts.len();
                        break;
                    }
                    Some((false, name)) => {
                        params.insert(name.to_string(), vec![path_parts[i].to_string()]);
                    }
                    None if seg != path_parts[i] => {
                        matches = false;
                        break;
                    }
                    None => {}
                }
                i += 1;
            }
            if matches && i == path_parts.len() {
                return Some((route.clone(), params));
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn router(paths: &[&str]) -> Router {
        Router::new(paths.iter().map(|s| s.to_string()).collect()).unwrap()
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
    fn rejects_conflicts_and_invalid_patterns() {
        for paths in [
            vec!["/blog/[id]", "/blog/[slug]"],
            vec!["/[...a]/x"],
            vec!["/[id]/[id]"],
            vec!["/[[...id]]"],
            vec!["/[..id]"],
        ] {
            assert!(
                Router::new(paths.iter().map(|s| s.to_string()).collect()).is_err(),
                "{paths:?}"
            );
        }
    }
}
