//! Bounded, process-local page cache with a monotonic freshness clock.
//!
//! The eviction order is a true O(1) LRU modelled on Next.js
//! `server/lib/lru-cache.ts`: a doubly-linked list (most recently used at
//! the head, least recently used at the tail) plus a hash map from key to
//! list node, so lookup, insert and eviction never scan the entries.
//! The list lives in an arena (`Vec<Option<Entry>>`) with a free list, so
//! nodes are addressed by slot index instead of pointers.
//!
//! Entry size is the Next.js `memory-cache.external.ts` estimate for a
//! page value: key length + body length. `total_size` is the sum of entry
//! sizes and is bounded by `max_bytes`; the entry count is bounded by
//! `max_entries` and each body by `max_body_bytes`.
use bytes::Bytes;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug, PartialEq, Eq)]
pub enum CacheResult {
    Fresh(Bytes),
    Stale(Bytes),
    Expired,
    Miss,
}

/// The Next.js `calculateSize` estimate for a page entry: key length +
/// body length. The key is part of the size because the cache stores the
/// key alongside the body; ignoring it would let many tiny bodies with
/// long keys grow the cache past `max_bytes` unaccounted.
fn entry_size(key: &str, body: &Bytes) -> usize {
    key.len().saturating_add(body.len())
}

struct Entry {
    key: String,
    body: Bytes,
    generated_at: Instant,
    fresh_for: Duration,
    stale_for: Duration,
    /// `entry_size(&key, &body)`; cached so eviction does not recompute it.
    size: usize,
    /// Previous node towards the head (most recently used), if any.
    prev: Option<usize>,
    /// Next node towards the tail (least recently used), if any.
    next: Option<usize>,
}

#[derive(Default)]
struct State {
    /// Key -> arena slot of the live node.
    index: HashMap<String, usize>,
    /// Arena of list nodes; `None` slots are on `free`.
    nodes: Vec<Option<Entry>>,
    /// Slots available for reuse.
    free: Vec<usize>,
    /// Most recently used node.
    head: Option<usize>,
    /// Least recently used node.
    tail: Option<usize>,
    /// Sum of live entry sizes; always `<= max_bytes` after an insert.
    total_size: usize,
}

impl State {
    /// Unlinks `slot` from the list; the node stays in the arena.
    fn detach(&mut self, slot: usize) {
        let (prev, next) = {
            let node = self.nodes[slot].as_ref().expect("live node");
            (node.prev, node.next)
        };
        match prev {
            Some(prev_slot) => {
                self.nodes[prev_slot].as_mut().expect("live node").next = next;
            }
            None => self.head = next,
        }
        match next {
            Some(next_slot) => {
                self.nodes[next_slot].as_mut().expect("live node").prev = prev;
            }
            None => self.tail = prev,
        }
        let node = self.nodes[slot].as_mut().expect("live node");
        node.prev = None;
        node.next = None;
    }

    /// Links `slot` at the head. The node must be detached.
    fn attach_head(&mut self, slot: usize) {
        let node = self.nodes[slot].as_mut().expect("live node");
        debug_assert!(node.prev.is_none() && node.next.is_none());
        node.next = self.head;
        if let Some(head_slot) = self.head {
            self.nodes[head_slot].as_mut().expect("live node").prev = Some(slot);
        } else {
            self.tail = Some(slot);
        }
        self.head = Some(slot);
    }

    /// Moves a live node to the head (marks it most recently used).
    fn move_to_head(&mut self, slot: usize) {
        if self.head == Some(slot) {
            return;
        }
        self.detach(slot);
        self.attach_head(slot);
    }

    /// Removes the node from the list and the arena, returning it. The
    /// caller removes the key from `index` and adjusts `total_size`.
    fn take(&mut self, slot: usize) -> Entry {
        self.detach(slot);
        let entry = self.nodes[slot].take().expect("live node");
        self.free.push(slot);
        entry
    }

    /// Removes and returns the least recently used node, if any.
    fn pop_tail(&mut self) -> Option<(String, Entry)> {
        let slot = self.tail?;
        let key = self.nodes[slot].as_ref().expect("live node").key.clone();
        Some((key, self.take(slot)))
    }
}

/// Capacity-eviction hook: evicted key and body. Registered with
/// `PageCache::set_on_evict`.
type EvictHook = Arc<dyn Fn(&str, Bytes) + Send + Sync>;

pub struct PageCache {
    state: Mutex<State>,
    max_entries: usize,
    max_bytes: usize,
    max_body_bytes: usize,
    /// Called with the evicted key and body for capacity evictions only.
    /// Explicit `remove()` calls and expired-entry drops do not fire it,
    /// matching Next.js `LRUCache.remove()` semantics. The hook runs after
    /// the state lock is released, so it may call back into the cache.
    on_evict: Mutex<Option<EvictHook>>,
}

impl PageCache {
    pub fn new(max_entries: usize, max_bytes: usize, max_body_bytes: usize) -> Self {
        Self {
            state: Mutex::new(State::default()),
            max_entries,
            max_bytes,
            max_body_bytes,
            on_evict: Mutex::new(None),
        }
    }

    /// Registers the capacity-eviction hook, replacing any previous one.
    pub fn set_on_evict<F>(&self, on_evict: F)
    where
        F: Fn(&str, Bytes) + Send + Sync + 'static,
    {
        *self.on_evict.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(on_evict));
    }

    /// Number of live entries.
    pub fn len(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .index
            .len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Sum of live entry sizes (key length + body length per entry).
    pub fn total_size(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .total_size
    }

    pub fn lookup(&self, key: &str) -> CacheResult {
        self.lookup_at(key, Instant::now())
    }

    pub fn remove(&self, key: &str) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let Some(&slot) = state.index.get(key) else {
            return false;
        };
        state.index.remove(key);
        let removed = state.take(slot);
        state.total_size -= removed.size;
        true
    }

    fn lookup_at(&self, key: &str, now: Instant) -> CacheResult {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let Some(&slot) = state.index.get(key) else {
            return CacheResult::Miss;
        };
        let entry = state.nodes[slot].as_ref().expect("live node");
        let age = now.saturating_duration_since(entry.generated_at);
        if age >= entry.fresh_for.saturating_add(entry.stale_for) {
            // Expired entries must not be kept alive by lookups: drop the
            // entry (and its bytes) instead of refreshing its LRU access.
            state.index.remove(key);
            let removed = state.take(slot);
            state.total_size -= removed.size;
            return CacheResult::Expired;
        }
        let fresh = age < entry.fresh_for;
        let body = entry.body.clone();
        state.move_to_head(slot);
        if fresh {
            CacheResult::Fresh(body)
        } else {
            CacheResult::Stale(body)
        }
    }

    pub fn insert(
        &self,
        key: String,
        body: Bytes,
        fresh_for: Duration,
        stale_for: Duration,
    ) -> bool {
        self.insert_at(key, body, fresh_for, stale_for, Instant::now())
    }

    fn insert_at(
        &self,
        key: String,
        body: Bytes,
        fresh_for: Duration,
        stale_for: Duration,
        now: Instant,
    ) -> bool {
        let size = entry_size(&key, &body);
        if key.len() > 4096 || self.max_entries == 0 || body.len() > self.max_body_bytes {
            return false;
        }
        if size == 0 {
            // A zero-size entry would never be evicted by the size budget
            // and could grow the entry count without bound; reject it.
            return false;
        }
        if size > self.max_bytes {
            // Next.js `LRUCache.set()` warns and refuses a single item
            // larger than the whole cache instead of evicting everything
            // for it. `log` has no sink in the embedding Python process,
            // so the warning goes to stderr, which the server drains.
            eprintln!(
                "[pynext-cache] refusing to cache {key:?}: entry size {size} exceeds max_bytes {}",
                self.max_bytes
            );
            return false;
        }
        let mut evicted: Vec<(String, Bytes)> = Vec::new();
        {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(&slot) = state.index.get(&key) {
                state.index.remove(&key);
                let previous = state.take(slot);
                state.total_size -= previous.size;
            }
            // Evict least recently used entries until the new entry fits
            // both budgets. Each step pops the tail in O(1); the loop runs
            // once per evicted entry, never a scan of the live set.
            while state.index.len() >= self.max_entries
                || state.total_size.saturating_add(size) > self.max_bytes
            {
                let Some((old_key, removed)) = state.pop_tail() else {
                    break;
                };
                state.index.remove(&old_key);
                state.total_size -= removed.size;
                evicted.push((old_key, removed.body));
            }
            let slot = match state.free.pop() {
                Some(slot) => slot,
                None => {
                    state.nodes.push(None);
                    state.nodes.len() - 1
                }
            };
            state.total_size += size;
            state.nodes[slot] = Some(Entry {
                key: key.clone(),
                body,
                generated_at: now,
                fresh_for,
                stale_for,
                size,
                prev: None,
                next: None,
            });
            state.attach_head(slot);
            state.index.insert(key, slot);
        }
        if !evicted.is_empty() {
            let hook = self
                .on_evict
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone();
            if let Some(hook) = hook {
                for (key, body) in evicted {
                    hook(&key, body);
                }
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    /// Walks the list head (MRU) -> tail (LRU) and checks the structural
    /// invariants: order matches `expected`, list length == index size ==
    /// live arena slots, and `total_size` is the sum of entry sizes.
    fn assert_invariants(state: &State, expected: &[&str]) {
        let mut order = Vec::new();
        let mut slot = state.head;
        let mut walked = 0;
        while let Some(current) = slot {
            let node = state.nodes[current].as_ref().expect("live node");
            order.push(node.key.as_str());
            slot = node.next;
            walked += 1;
            assert!(walked <= state.index.len(), "list cycle detected");
        }
        assert_eq!(order, expected, "MRU -> LRU order");
        assert_eq!(walked, state.index.len());
        assert_eq!(
            state.nodes.iter().filter(|n| n.is_some()).count(),
            state.index.len(),
            "arena live slots"
        );
        assert_eq!(
            state.total_size,
            state
                .nodes
                .iter()
                .flatten()
                .map(|entry| entry.size)
                .sum::<usize>()
        );
        // Head/tail pointers agree with the walked ends.
        if expected.is_empty() {
            assert!(state.head.is_none() && state.tail.is_none());
        } else {
            assert_eq!(
                state.nodes[state.head.unwrap()].as_ref().unwrap().key,
                expected[0]
            );
            assert_eq!(
                state.nodes[state.tail.unwrap()].as_ref().unwrap().key,
                expected[expected.len() - 1]
            );
        }
    }

    #[test]
    fn removal_updates_capacity_and_is_idempotent() {
        let cache = PageCache::new(1, 4, 3);
        assert!(cache.insert(
            "a".into(),
            Bytes::from_static(b"one"),
            Duration::from_secs(10),
            Duration::ZERO
        ));
        assert!(cache.remove("a"));
        assert!(!cache.remove("a"));
        assert_eq!(cache.lookup("a"), CacheResult::Miss);
        assert!(cache.insert(
            "b".into(),
            Bytes::from_static(b"two"),
            Duration::from_secs(10),
            Duration::ZERO
        ));
        assert_eq!(
            cache.lookup("b"),
            CacheResult::Fresh(Bytes::from_static(b"two"))
        );
        assert_invariants(&cache.state.lock().unwrap(), &["b"]);
    }

    #[test]
    fn freshness_transitions_without_sleep() {
        let cache = PageCache::new(2, 100, 100);
        let now = Instant::now();
        assert!(cache.insert_at(
            "/a".into(),
            Bytes::from_static(b"one"),
            Duration::from_secs(2),
            Duration::from_secs(3),
            now
        ));
        assert_eq!(
            cache.lookup_at("/a", now + Duration::from_secs(1)),
            CacheResult::Fresh(Bytes::from_static(b"one"))
        );
        assert_eq!(
            cache.lookup_at("/a", now + Duration::from_secs(2)),
            CacheResult::Stale(Bytes::from_static(b"one"))
        );
        assert_eq!(
            cache.lookup_at("/a", now + Duration::from_secs(5)),
            CacheResult::Expired
        );
        assert_eq!(cache.lookup_at("/other", now), CacheResult::Miss);
    }

    #[test]
    fn expired_lookup_removes_entry_instead_of_refreshing_it() {
        let cache = PageCache::new(2, 100, 100);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at(
            "/a".into(),
            Bytes::from_static(b"one"),
            Duration::from_secs(2),
            Duration::from_secs(3),
            now
        ));
        assert!(cache.insert_at("/b".into(), Bytes::from_static(b"two"), ttl, ttl, now));
        assert_eq!(
            cache.lookup_at("/a", now + Duration::from_secs(5)),
            CacheResult::Expired
        );
        // The expired entry is gone: a later lookup is a miss, its bytes no
        // longer count, and it cannot be evicted in place of a live entry.
        assert_eq!(
            cache.lookup_at("/a", now + Duration::from_secs(5)),
            CacheResult::Miss
        );
        let state = cache.state.lock().unwrap();
        assert!(!state.index.contains_key("/a"));
        // "/b" (2) + "two" (3) = 5.
        assert_eq!(state.total_size, 5);
        assert_eq!(state.index.len(), 1);
        assert_invariants(&state, &["/b"]);
    }

    #[test]
    fn eviction_and_oversized_writes_preserve_previous_value() {
        let cache = PageCache::new(2, 8, 4);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"one"), ttl, ttl, now));
        assert!(cache.insert_at("b".into(), Bytes::from_static(b"two"), ttl, ttl, now));
        assert!(matches!(cache.lookup_at("a", now), CacheResult::Fresh(_)));
        assert!(cache.insert_at("c".into(), Bytes::from_static(b"tri"), ttl, ttl, now));
        assert_eq!(cache.lookup_at("b", now), CacheResult::Miss);
        assert!(!cache.insert_at("a".into(), Bytes::from_static(b"large"), ttl, ttl, now));
        assert_eq!(
            cache.lookup_at("a", now),
            CacheResult::Fresh(Bytes::from_static(b"one"))
        );
        assert_invariants(&cache.state.lock().unwrap(), &["a", "c"]);
    }

    #[test]
    fn lru_order_evicts_least_recently_used() {
        let cache = PageCache::new(2, 100, 100);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"one"), ttl, ttl, now));
        assert!(cache.insert_at("b".into(), Bytes::from_static(b"two"), ttl, ttl, now));
        assert_invariants(&cache.state.lock().unwrap(), &["b", "a"]);
        // Touching "a" makes it most recently used, so "b" is evicted next.
        assert!(matches!(cache.lookup_at("a", now), CacheResult::Fresh(_)));
        assert_invariants(&cache.state.lock().unwrap(), &["a", "b"]);
        assert!(cache.insert_at("c".into(), Bytes::from_static(b"tri"), ttl, ttl, now));
        assert_eq!(cache.lookup_at("b", now), CacheResult::Miss);
        assert_eq!(
            cache.lookup_at("a", now),
            CacheResult::Fresh(Bytes::from_static(b"one"))
        );
        assert_eq!(
            cache.lookup_at("c", now),
            CacheResult::Fresh(Bytes::from_static(b"tri"))
        );
        assert_invariants(&cache.state.lock().unwrap(), &["c", "a"]);
    }

    #[test]
    fn size_based_eviction_accounts_for_key_length() {
        // Sizes are key + body: "aa"+4 = 6, "bbbb"+4 = 8. Bodies alone
        // (4 + 4 = 8) would fit in max_bytes=10; with keys they do not.
        let cache = PageCache::new(100, 10, 100);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at("aa".into(), Bytes::from_static(b"1234"), ttl, ttl, now));
        assert_eq!(cache.total_size(), 6);
        assert!(cache.insert_at("bbbb".into(), Bytes::from_static(b"1234"), ttl, ttl, now));
        assert_eq!(cache.lookup_at("aa", now), CacheResult::Miss);
        assert_eq!(
            cache.lookup_at("bbbb", now),
            CacheResult::Fresh(Bytes::from_static(b"1234"))
        );
        assert_eq!(cache.total_size(), 8);
        assert_invariants(&cache.state.lock().unwrap(), &["bbbb"]);
    }

    #[test]
    fn zero_size_and_oversized_single_items_are_rejected() {
        let cache = PageCache::new(10, 8, 100);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        // Empty key + empty body has size 0 and would never be evicted by
        // the size budget.
        assert!(!cache.insert_at(String::new(), Bytes::new(), ttl, ttl, now));
        assert!(cache.is_empty());
        // A single item larger than the whole cache is refused (with a
        // warning on stderr) instead of evicting everything for it.
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"one"), ttl, ttl, now));
        assert!(!cache.insert_at("big".into(), Bytes::from_static(b"123456"), ttl, ttl, now));
        assert_eq!(
            cache.lookup_at("a", now),
            CacheResult::Fresh(Bytes::from_static(b"one"))
        );
        assert_eq!(cache.len(), 1);
        assert_invariants(&cache.state.lock().unwrap(), &["a"]);
    }

    #[test]
    fn on_evict_hook_fires_only_for_capacity_evictions() {
        let cache = PageCache::new(2, 100, 100);
        let evicted = Arc::new(StdMutex::new(Vec::new()));
        let recorded = Arc::clone(&evicted);
        cache.set_on_evict(move |key, body| {
            recorded.lock().unwrap().push((key.to_owned(), body.len()));
        });
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"one"), ttl, ttl, now));
        assert!(cache.insert_at("b".into(), Bytes::from_static(b"two"), ttl, ttl, now));
        // Capacity eviction fires the hook.
        assert!(cache.insert_at("c".into(), Bytes::from_static(b"tri"), ttl, ttl, now));
        assert_eq!(*evicted.lock().unwrap(), vec![("a".to_owned(), 3)]);
        // Explicit removal does not fire the hook.
        assert!(cache.remove("b"));
        assert_eq!(*evicted.lock().unwrap(), vec![("a".to_owned(), 3)]);
        // Expiry on lookup does not fire the hook either.
        assert!(cache.insert_at(
            "d".into(),
            Bytes::from_static(b"four"),
            Duration::from_secs(1),
            Duration::ZERO,
            now
        ));
        assert_eq!(
            cache.lookup_at("d", now + Duration::from_secs(2)),
            CacheResult::Expired
        );
        assert_eq!(*evicted.lock().unwrap(), vec![("a".to_owned(), 3)]);
    }

    #[test]
    fn updating_an_entry_refreshes_recency_and_size() {
        let cache = PageCache::new(2, 12, 100);
        let now = Instant::now();
        let ttl = Duration::from_secs(10);
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"one"), ttl, ttl, now));
        assert!(cache.insert_at("b".into(), Bytes::from_static(b"two"), ttl, ttl, now));
        assert_eq!(cache.total_size(), 8);
        // Rewriting "a" with a bigger body moves it to the head and grows
        // its accounted size (1 + 6 = 7; total 11).
        assert!(cache.insert_at("a".into(), Bytes::from_static(b"123456"), ttl, ttl, now));
        assert_eq!(cache.total_size(), 11);
        assert_invariants(&cache.state.lock().unwrap(), &["a", "b"]);
        // The next insert must evict "b" (now least recently used), not "a".
        assert!(cache.insert_at("c".into(), Bytes::from_static(b"tri"), ttl, ttl, now));
        assert_eq!(cache.lookup_at("b", now), CacheResult::Miss);
        assert_eq!(
            cache.lookup_at("a", now),
            CacheResult::Fresh(Bytes::from_static(b"123456"))
        );
        assert_eq!(cache.total_size(), 11);
        assert_invariants(&cache.state.lock().unwrap(), &["a", "c"]);
    }

    #[test]
    fn concurrent_reads_and_writes_preserve_limits() {
        let cache = std::sync::Arc::new(PageCache::new(32, 256, 16));
        let mut workers = Vec::new();
        for worker in 0..8 {
            let cache = cache.clone();
            workers.push(std::thread::spawn(move || {
                for iteration in 0..1000 {
                    let key = format!("{worker}-{}", iteration % 20);
                    cache.insert(
                        key.clone(),
                        Bytes::from_static(b"12345678"),
                        Duration::from_secs(10),
                        Duration::from_secs(10),
                    );
                    let _ = cache.lookup(&key);
                }
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }
        let state = cache.state.lock().unwrap();
        assert!(state.index.len() <= 32);
        assert!(state.total_size <= 256);
        assert_eq!(
            state.total_size,
            state
                .nodes
                .iter()
                .flatten()
                .map(|entry| entry.size)
                .sum::<usize>()
        );
        // List walk agrees with the index (no lost or duplicated nodes).
        let mut walked = 0;
        let mut slot = state.head;
        while let Some(current) = slot {
            walked += 1;
            slot = state.nodes[current].as_ref().unwrap().next;
        }
        assert_eq!(walked, state.index.len());
    }
}
