//! Bounded, process-local page cache with a monotonic freshness clock.
use bytes::Bytes;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Debug, PartialEq, Eq)]
pub enum CacheResult {
    Fresh(Bytes),
    Stale(Bytes),
    Expired,
    Miss,
}

struct Entry {
    body: Bytes,
    generated_at: Instant,
    fresh_for: Duration,
    stale_for: Duration,
    access: u64,
}

#[derive(Default)]
struct State {
    entries: HashMap<String, Entry>,
    total_bytes: usize,
    clock: u64,
}

pub struct PageCache {
    state: Mutex<State>,
    max_entries: usize,
    max_bytes: usize,
    max_body_bytes: usize,
}

impl PageCache {
    pub fn new(max_entries: usize, max_bytes: usize, max_body_bytes: usize) -> Self {
        Self {
            state: Mutex::new(State::default()),
            max_entries,
            max_bytes,
            max_body_bytes,
        }
    }

    pub fn lookup(&self, key: &str) -> CacheResult {
        self.lookup_at(key, Instant::now())
    }

    fn lookup_at(&self, key: &str, now: Instant) -> CacheResult {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.clock = state.clock.wrapping_add(1);
        let access = state.clock;
        let Some(entry) = state.entries.get_mut(key) else {
            return CacheResult::Miss;
        };
        entry.access = access;
        let age = now.saturating_duration_since(entry.generated_at);
        if age < entry.fresh_for {
            CacheResult::Fresh(entry.body.clone())
        } else if age < entry.fresh_for.saturating_add(entry.stale_for) {
            CacheResult::Stale(entry.body.clone())
        } else {
            CacheResult::Expired
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
        if key.len() > 4096
            || self.max_entries == 0
            || body.len() > self.max_body_bytes
            || body.len() > self.max_bytes
        {
            return false;
        }
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(previous) = state.entries.remove(&key) {
            state.total_bytes -= previous.body.len();
        }
        while state.entries.len() >= self.max_entries
            || body.len() > self.max_bytes - state.total_bytes
        {
            let Some(oldest) = state
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.access)
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            if let Some(removed) = state.entries.remove(&oldest) {
                state.total_bytes -= removed.body.len();
            }
        }
        state.clock = state.clock.wrapping_add(1);
        let access = state.clock;
        state.total_bytes += body.len();
        state.entries.insert(
            key,
            Entry {
                body,
                generated_at: now,
                fresh_for,
                stale_for,
                access,
            },
        );
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn eviction_and_oversized_writes_preserve_previous_value() {
        let cache = PageCache::new(2, 6, 4);
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
        assert!(state.entries.len() <= 32);
        assert!(state.total_bytes <= 256);
        assert_eq!(
            state.total_bytes,
            state
                .entries
                .values()
                .map(|entry| entry.body.len())
                .sum::<usize>()
        );
    }
}
