//! Aggregation done after the run: peak, leaks, temporaries, timeline.

use std::collections::HashMap;

use pyo3::prelude::*;

#[derive(Clone, Copy)]
pub struct Event {
    pub t_ns: u64,
    pub delta: i64,
    pub stack: u32,
    /// From malloc/mmap in native code rather than CPython's allocators.
    pub native: bool,
}

/// Bytes and block count for one stack (or one site).
#[derive(Clone, Copy, Default)]
pub struct Agg {
    pub bytes: u64,
    pub native_bytes: u64,
    pub count: u64,
}

impl Agg {
    fn add(&mut self, other: &Agg) {
        self.bytes += other.bytes;
        self.native_bytes += other.native_bytes;
        self.count += other.count;
    }
}

#[derive(Clone)]
pub struct Frame {
    pub file: String,
    pub func: String,
    pub line: i32,
}

/// One row of a view: an allocation site in user code.
#[derive(Clone)]
pub struct SiteRow {
    pub func: String,
    pub file: String,
    pub line: i32,
    pub bytes: u64,
    pub native_bytes: u64,
    pub count: u64,
}

#[pyclass(module = "heapwise._native", frozen)]
pub struct Capture {
    pub frames: Vec<Frame>,
    pub stacks: Vec<Vec<u32>>,
    pub events: Vec<Event>,
    pub live_at_end: HashMap<u32, Agg>,
    pub temporaries: HashMap<u32, u64>,
    pub duration_ns: u64,
    pub alloc_count: u64,
    pub alloc_bytes: u64,
    pub sample_rate: u64,
    pub peak_bytes: u64,
    pub peak_t_ns: u64,
    pub peak_native_bytes: u64,
    pub peak_by_stack: HashMap<u32, Agg>,
    /// Whether native allocations were intercepted on this platform.
    pub native_supported: bool,
}

impl Capture {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        frames: Vec<Frame>,
        stacks: Vec<Vec<u32>>,
        events: Vec<Event>,
        live_at_end: HashMap<u32, Agg>,
        temporaries: HashMap<u32, u64>,
        duration_ns: u64,
        alloc_count: u64,
        alloc_bytes: u64,
        sample_rate: u64,
        native_supported: bool,
    ) -> Self {
        // Pass 1: find the high-water mark.
        let (mut cur, mut peak, mut peak_idx, mut peak_t) = (0i64, 0i64, None, 0u64);
        for (i, e) in events.iter().enumerate() {
            cur += e.delta;
            if cur > peak {
                peak = cur;
                peak_idx = Some(i);
                peak_t = e.t_ns;
            }
        }
        // Pass 2: replay up to it to see what was live.
        // (bytes, native bytes, blocks)
        let mut by_stack: HashMap<u32, (i64, i64, i64)> = HashMap::new();
        if let Some(idx) = peak_idx {
            for e in &events[..=idx] {
                let s = by_stack.entry(e.stack).or_insert((0, 0, 0));
                s.0 += e.delta;
                if e.native {
                    s.1 += e.delta;
                }
                s.2 += e.delta.signum();
            }
        }
        let peak_by_stack: HashMap<u32, Agg> = by_stack
            .into_iter()
            .filter(|(_, (b, _, _))| *b > 0)
            .map(|(k, (b, n, c))| {
                (k, Agg { bytes: b as u64, native_bytes: n.max(0) as u64, count: c.max(0) as u64 })
            })
            .collect();
        let peak_native_bytes = peak_by_stack.values().map(|a| a.native_bytes).sum();

        Capture {
            frames,
            stacks,
            events,
            live_at_end,
            temporaries,
            duration_ns,
            alloc_count,
            alloc_bytes,
            sample_rate,
            peak_bytes: peak.max(0) as u64,
            peak_t_ns: peak_t,
            peak_native_bytes,
            peak_by_stack,
            native_supported,
        }
    }

    /// The innermost frame that is not excluded (stdlib, site-packages, us).
    fn site_of(&self, stack: u32, exclude: &[String]) -> (String, String, i32) {
        let frames = &self.stacks[stack as usize];
        let pick = frames
            .iter()
            .map(|&f| &self.frames[f as usize])
            .find(|f| !is_excluded(&f.file, exclude))
            .or_else(|| frames.first().map(|&f| &self.frames[f as usize]));
        match pick {
            Some(f) => (f.func.clone(), f.file.clone(), f.line),
            None => ("<no Python stack>".into(), String::new(), 0),
        }
    }

    pub fn rows(&self, view: &str, exclude: &[String]) -> Vec<SiteRow> {
        let source: Vec<(u32, Agg)> = match view {
            "peak" => self.peak_by_stack.iter().map(|(&s, a)| (s, *a)).collect(),
            "leaks" => self.live_at_end.iter().map(|(&s, a)| (s, *a)).collect(),
            "temporaries" => {
                self.temporaries.iter().map(|(&s, &c)| (s, Agg { count: c, ..Agg::default() })).collect()
            }
            _ => Vec::new(),
        };
        let mut sites: HashMap<(String, String, i32), Agg> = HashMap::new();
        for (stack, agg) in source {
            sites.entry(self.site_of(stack, exclude)).or_default().add(&agg);
        }
        let mut rows: Vec<SiteRow> = sites
            .into_iter()
            .map(|((func, file, line), a)| SiteRow {
                func,
                file,
                line,
                bytes: a.bytes,
                native_bytes: a.native_bytes,
                count: a.count,
            })
            .collect();
        if view == "temporaries" {
            rows.sort_by(|a, b| b.count.cmp(&a.count).then(a.func.cmp(&b.func)));
        } else {
            rows.sort_by(|a, b| b.bytes.cmp(&a.bytes).then(a.func.cmp(&b.func)));
        }
        rows
    }

    /// Highest live bytes in each of `buckets` equal time slices.
    pub fn timeline_values(&self, buckets: usize) -> Vec<u64> {
        let buckets = buckets.max(1);
        let span = self.duration_ns.max(1);
        let mut out = vec![0u64; buckets];
        let mut cur = 0i64;
        let mut last_bucket = 0;
        for e in &self.events {
            let b = ((e.t_ns as u128 * buckets as u128) / span as u128).min(buckets as u128 - 1) as usize;
            for slot in out.iter_mut().take(b).skip(last_bucket + 1) {
                *slot = cur.max(0) as u64; // carry forward through quiet slices
            }
            cur += e.delta;
            out[b] = out[b].max(cur.max(0) as u64);
            last_bucket = b;
        }
        for slot in out.iter_mut().skip(last_bucket + 1) {
            *slot = cur.max(0) as u64;
        }
        out
    }
}

pub fn is_excluded(file: &str, exclude: &[String]) -> bool {
    file.starts_with('<') || exclude.iter().any(|p| !p.is_empty() && file.starts_with(p.as_str()))
}

#[pymethods]
impl Capture {
    #[getter]
    fn peak_bytes(&self) -> u64 {
        self.peak_bytes
    }
    #[getter]
    fn peak_seconds(&self) -> f64 {
        self.peak_t_ns as f64 / 1e9
    }
    #[getter]
    fn duration_seconds(&self) -> f64 {
        self.duration_ns as f64 / 1e9
    }
    #[getter]
    fn allocation_count(&self) -> u64 {
        self.alloc_count
    }
    #[getter]
    fn allocated_bytes(&self) -> u64 {
        self.alloc_bytes
    }
    #[getter]
    fn sample_rate(&self) -> u64 {
        self.sample_rate
    }
    #[getter]
    fn leaked_bytes(&self) -> u64 {
        self.live_at_end.values().map(|a| a.bytes).sum()
    }
    /// Part of the peak that came from native code (malloc/mmap).
    #[getter]
    fn peak_native_bytes(&self) -> u64 {
        self.peak_native_bytes
    }
    #[getter]
    fn native_supported(&self) -> bool {
        self.native_supported
    }

    /// Rows for one view ("peak", "leaks" or "temporaries"), grouped by the
    /// innermost frame outside `exclude`:
    /// (function, file, line, bytes, count, native_bytes).
    #[pyo3(signature = (view, exclude = Vec::new(), limit = 10))]
    #[allow(clippy::type_complexity)]
    fn top(
        &self,
        view: &str,
        exclude: Vec<String>,
        limit: usize,
    ) -> PyResult<Vec<(String, String, i32, u64, u64, u64)>> {
        if !matches!(view, "peak" | "leaks" | "temporaries") {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "view must be 'peak', 'leaks' or 'temporaries'",
            ));
        }
        Ok(self
            .rows(view, &exclude)
            .into_iter()
            .take(limit)
            .map(|r| (r.func, r.file, r.line, r.bytes, r.count, r.native_bytes))
            .collect())
    }

    #[pyo3(signature = (buckets = 60))]
    fn timeline(&self, buckets: usize) -> Vec<u64> {
        self.timeline_values(buckets)
    }

    /// Findings-first plain-text report.
    #[pyo3(signature = (title, exclude = Vec::new(), cwd = None, top = 5))]
    fn summary(&self, title: &str, exclude: Vec<String>, cwd: Option<String>, top: usize) -> String {
        crate::report::summary(self, title, &exclude, cwd.as_deref(), top)
    }
}
