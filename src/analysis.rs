//! Aggregation done after the run: peak, leaks, temporaries, timeline.

use std::collections::HashMap;

use pyo3::prelude::*;

use crate::ledger::Timeline;

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
    /// Stack trie: node i is (parent node, innermost frame); node 0 is the
    /// empty stack and every parent has a lower index.
    pub nodes: Vec<(u32, u32)>,
    pub timeline: Timeline,
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

/// Frame ids of one stack, innermost first.
pub struct StackFrames<'a> {
    nodes: &'a [(u32, u32)],
    at: u32,
}

impl Iterator for StackFrames<'_> {
    type Item = u32;
    fn next(&mut self) -> Option<u32> {
        if self.at == 0 {
            return None;
        }
        let (parent, frame) = self.nodes[self.at as usize];
        self.at = parent;
        Some(frame)
    }
}

impl Capture {
    pub fn stack_frames(&self, stack: u32) -> StackFrames<'_> {
        StackFrames { nodes: &self.nodes, at: stack }
    }

    /// The innermost frame that is not excluded (stdlib, site-packages, us).
    fn site_of(&self, stack: u32, exclude: &[String]) -> (String, String, i32) {
        let pick = self
            .stack_frames(stack)
            .map(|f| &self.frames[f as usize])
            .find(|f| !is_excluded(&f.file, exclude))
            .or_else(|| self.stack_frames(stack).next().map(|f| &self.frames[f as usize]));
        match pick {
            Some(f) if !is_excluded(&f.file, exclude) => (f.func.clone(), f.file.clone(), f.line),
            // Only runpy/importlib below the launcher: compiling and starting
            // the script, before any of the user's own frames exist.
            Some(_) if self.stack_frames(stack).any(|f| is_runpy(&self.frames[f as usize].file)) => {
                (STARTUP.into(), String::new(), 0)
            }
            Some(f) => (f.func.clone(), f.file.clone(), f.line),
            None => (NO_STACK.into(), String::new(), 0),
        }
    }

    fn view_source(&self, view: &str) -> Vec<(u32, Agg)> {
        match view {
            "peak" => self.peak_by_stack.iter().map(|(&s, a)| (s, *a)).collect(),
            "leaks" => self.live_at_end.iter().map(|(&s, a)| (s, *a)).collect(),
            "temporaries" => {
                self.temporaries.iter().map(|(&s, &c)| (s, Agg { count: c, ..Agg::default() })).collect()
            }
            _ => Vec::new(),
        }
    }

    pub fn rows(&self, view: &str, exclude: &[String]) -> Vec<SiteRow> {
        let source = self.view_source(view);
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
        self.timeline.resample(buckets, self.duration_ns)
    }
}

/// Site label for allocations made while the script was being loaded.
pub const STARTUP: &str = "<script startup>";
/// Site label for allocations with no Python frame at all.
pub const NO_STACK: &str = "<no Python stack>";

fn is_runpy(file: &str) -> bool {
    file == "<frozen runpy>" || file.ends_with("/runpy.py") || file.ends_with("\\runpy.py")
}

fn check_view(view: &str) -> PyResult<()> {
    if matches!(view, "peak" | "leaks" | "temporaries") {
        Ok(())
    } else {
        Err(pyo3::exceptions::PyValueError::new_err("view must be 'peak', 'leaks' or 'temporaries'"))
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
        check_view(view)?;
        Ok(self
            .rows(view, &exclude)
            .into_iter()
            .take(limit)
            .map(|r| (r.func, r.file, r.line, r.bytes, r.count, r.native_bytes))
            .collect())
    }

    /// Every frame the capture refers to, as (function, file, line).
    /// Indexes into this list are what `stacks()` returns.
    fn frames(&self) -> Vec<(String, String, i32)> {
        self.frames.iter().map(|f| (f.func.clone(), f.file.clone(), f.line)).collect()
    }

    /// Full call stacks for one view, outermost frame first:
    /// (frame indexes, bytes, count, native_bytes). For flame graphs.
    #[allow(clippy::type_complexity)]
    fn stacks(&self, view: &str) -> PyResult<Vec<(Vec<u32>, u64, u64, u64)>> {
        check_view(view)?;
        let mut out: Vec<_> = self
            .view_source(view)
            .into_iter()
            .filter(|(_, a)| a.bytes > 0 || a.count > 0)
            .map(|(s, a)| {
                let mut frames: Vec<u32> = self.stack_frames(s).collect();
                frames.reverse();
                (frames, a.bytes, a.count, a.native_bytes)
            })
            .collect();
        out.sort_by(|a, b| b.1.cmp(&a.1).then(b.2.cmp(&a.2)).then(a.0.cmp(&b.0)));
        Ok(out)
    }

    /// The stack trie as (parent node, frame index) pairs; node 0 is the
    /// empty stack. Compact form of `stacks()` for large captures.
    fn stack_nodes(&self) -> Vec<(u32, u32)> {
        self.nodes.clone()
    }

    /// One view keyed by trie node: (node, bytes, count, native_bytes).
    #[allow(clippy::type_complexity)]
    fn stack_values(&self, view: &str) -> PyResult<Vec<(u32, u64, u64, u64)>> {
        check_view(view)?;
        let mut out: Vec<_> = self
            .view_source(view)
            .into_iter()
            .filter(|(_, a)| a.bytes > 0 || a.count > 0)
            .map(|(s, a)| (s, a.bytes, a.count, a.native_bytes))
            .collect();
        out.sort_unstable_by(|a, b| b.1.cmp(&a.1).then(b.2.cmp(&a.2)).then(a.0.cmp(&b.0)));
        Ok(out)
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
