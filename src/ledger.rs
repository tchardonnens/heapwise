//! Online accounting: peak, what was live at the peak, and memory over time,
//! updated per event in O(1) so a capture's memory does not grow with the
//! length of the run, only with the number of distinct stacks.

use std::collections::HashMap;

use crate::analysis::Agg;

#[derive(Clone, Copy, Default)]
struct Live {
    bytes: i64,
    native: i64,
    count: i64,
}

#[derive(Clone, Copy, Default)]
struct StackAcc {
    cur: Live,
    /// This stack's value at the latest peak, valid once it has changed
    /// since then (see `apply`).
    at_peak: Live,
    last_change: u64,
}

#[derive(Default)]
pub struct Ledger {
    accs: Vec<StackAcc>,
    seq: u64,
    total: i64,
    peak: i64,
    peak_seq: u64,
    peak_t_ns: u64,
    pub timeline: Timeline,
}

impl Ledger {
    /// Record `delta` bytes on `stack`. `now_ns` may lag by a few events.
    #[inline]
    pub fn apply(&mut self, stack: u32, delta: i64, native: bool, now_ns: u64) {
        let i = stack as usize;
        if i >= self.accs.len() {
            self.accs.resize(i + 1, StackAcc::default());
        }
        self.seq += 1;
        let a = &mut self.accs[i];
        // A stack's value at the latest peak is its value just before its
        // first change after that peak, or its current value if it has not
        // changed since. Snapshot lazily on that first change.
        if a.last_change <= self.peak_seq {
            a.at_peak = a.cur;
        }
        a.cur.bytes += delta;
        if native {
            a.cur.native += delta;
        }
        a.cur.count += delta.signum();
        a.last_change = self.seq;
        self.total += delta;
        if self.total > self.peak {
            self.peak = self.total;
            self.peak_seq = self.seq;
            self.peak_t_ns = now_ns;
        }
        self.timeline.observe(self.total);
    }

    pub fn peak_bytes(&self) -> u64 {
        self.peak.max(0) as u64
    }

    pub fn peak_t_ns(&self) -> u64 {
        self.peak_t_ns
    }

    fn collect(&self, pick: impl Fn(&StackAcc) -> Live) -> HashMap<u32, Agg> {
        self.accs
            .iter()
            .enumerate()
            .filter_map(|(i, a)| {
                let v = pick(a);
                (v.bytes > 0).then(|| {
                    (i as u32, Agg { bytes: v.bytes as u64, native_bytes: v.native.max(0) as u64, count: v.count.max(0) as u64 })
                })
            })
            .collect()
    }

    pub fn at_peak(&self) -> HashMap<u32, Agg> {
        let peak_seq = self.peak_seq;
        self.collect(|a| if a.last_change <= peak_seq { a.cur } else { a.at_peak })
    }

    pub fn live_now(&self) -> HashMap<u32, Agg> {
        self.collect(|a| a.cur)
    }
}

/// Highest live total per time slot. Slots double in width whenever the run
/// outgrows them, so a run of any length takes at most `SLOTS` values.
pub struct Timeline {
    width_ns: u64,
    slots: Vec<u64>,
    pending_max: i64,
    last: i64,
    /// Live total at the end of the previous flush.
    level: i64,
}

const SLOTS: usize = 1024;

impl Default for Timeline {
    fn default() -> Self {
        Timeline { width_ns: 1_000, slots: Vec::new(), pending_max: 0, last: 0, level: 0 }
    }
}

impl Timeline {
    #[inline]
    fn observe(&mut self, total: i64) {
        self.pending_max = self.pending_max.max(total);
        self.last = total;
    }

    /// Fold the events seen since the previous flush into the slot for `now_ns`.
    pub fn flush(&mut self, now_ns: u64) {
        while now_ns / self.width_ns >= SLOTS as u64 {
            self.slots = self.slots.chunks(2).map(|c| c.iter().copied().max().unwrap_or(0)).collect();
            self.width_ns *= 2;
        }
        let b = (now_ns / self.width_ns) as usize;
        // Slots skipped since the last flush held the level it ended at.
        while self.slots.len() <= b {
            self.slots.push(self.level.max(0) as u64);
        }
        self.slots[b] = self.slots[b].max(self.pending_max.max(0) as u64);
        self.level = self.last;
        self.pending_max = self.last;
    }

    /// Highest value in each of `buckets` equal slices of `[0, duration_ns]`.
    pub fn resample(&self, buckets: usize, duration_ns: u64) -> Vec<u64> {
        let buckets = buckets.max(1);
        if self.slots.is_empty() {
            return vec![0; buckets];
        }
        let span = duration_ns.max(1) as u128;
        let w = self.width_ns as u128;
        let n = self.slots.len();
        (0..buckets)
            .map(|j| {
                let t0 = span * j as u128 / buckets as u128;
                let t1 = span * (j as u128 + 1) / buckets as u128;
                let lo = ((t0 / w) as usize).min(n - 1);
                let hi = (t1.div_ceil(w) as usize).clamp(lo + 1, n);
                self.slots[lo..hi].iter().copied().max().unwrap_or(0)
            })
            .collect()
    }
}
