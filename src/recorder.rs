//! Cheap recording on the allocation path.
//!
//! Each event does O(1) work: intern the stack, update the live map and the
//! online ledger (peak, live-at-peak, timeline). Nothing grows with the
//! number of events, so long runs cost no more memory than short ones.
//! Symbolization (names, line numbers) waits until the end.

use std::collections::HashMap;

use rustc_hash::{FxHashMap, FxHashSet};
use std::sync::{Mutex, MutexGuard};
use std::time::Instant;

use pyo3::ffi;

use crate::analysis::{Capture, Frame};
use crate::ledger::Ledger;

struct State {
    start: Instant,
    /// Code objects we hold a strong reference to, so each pointer stays
    /// unique for the whole capture.
    codes: FxHashSet<usize>,
    frame_ids: FxHashMap<(usize, i32), u32>,
    frames: Vec<(usize, i32)>,
    /// Stacks as a trie: (parent stack, frame) -> stack. Node 0 is the empty
    /// stack. Interning walks outermost to innermost, one small key per frame.
    stack_ids: FxHashMap<(u32, u32), u32>,
    stack_nodes: Vec<(u32, u32)>,
    live: FxHashMap<usize, (u64, u32, bool)>,
    ledger: Ledger,
    events: u64,
    now_ns: u64,
    temporaries: FxHashMap<u32, u64>,
    alloc_count: u64,
    alloc_bytes: u64,
}

impl State {
    fn push_alloc(&mut self, addr: usize, bytes: u64, stack: u32, native: bool) {
        if let Some((old_bytes, old_stack, old_native)) = self.live.insert(addr, (bytes, stack, native)) {
            // A free we never saw (e.g. memory released inside libc).
            self.ledger.apply(old_stack, -(old_bytes as i64), old_native, self.now_ns);
        }
        self.ledger.apply(stack, bytes as i64, native, self.now_ns);
        self.alloc_count += 1;
        self.alloc_bytes += bytes;
    }

    /// Read the clock only every 64 events; times in between take the
    /// last reading.
    #[inline]
    fn tick(&mut self) {
        if self.events.is_multiple_of(64) {
            self.now_ns = self.start.elapsed().as_nanos() as u64;
            self.ledger.timeline.flush(self.now_ns);
        }
        self.events += 1;
    }
}

static STATE: Mutex<Option<State>> = Mutex::new(None);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn begin() {
    *lock(&STATE) = Some(State {
        start: Instant::now(),
        codes: FxHashSet::default(),
        frame_ids: FxHashMap::default(),
        frames: Vec::new(),
        stack_ids: FxHashMap::default(),
        stack_nodes: vec![(0, u32::MAX)],
        live: FxHashMap::default(),
        ledger: Ledger::default(),
        events: 0,
        now_ns: 0,
        temporaries: FxHashMap::default(),
        alloc_count: 0,
        alloc_bytes: 0,
    });
}

unsafe fn attr_string(obj: *mut ffi::PyObject, name: &std::ffi::CStr) -> String {
    let value = ffi::PyObject_GetAttrString(obj, name.as_ptr());
    if value.is_null() {
        ffi::PyErr_Clear();
        return String::from("?");
    }
    let mut len: ffi::Py_ssize_t = 0;
    let data = ffi::PyUnicode_AsUTF8AndSize(value, &mut len);
    let s = if data.is_null() {
        ffi::PyErr_Clear();
        String::from("?")
    } else {
        let bytes = std::slice::from_raw_parts(data as *const u8, len as usize);
        String::from_utf8_lossy(bytes).into_owned()
    };
    ffi::Py_DECREF(value);
    s
}

/// `raw_frames` holds (code object, bytecode offset) pairs, innermost first,
/// each code object with a new reference that this function takes over.
///
/// `known_parent` is the stack id of `raw_frames[1..]` when the caller has it
/// cached; then only the innermost frame is interned. Returns the ids of the
/// full stack and of `raw_frames[1..]`.
pub unsafe fn record_alloc(
    addr: usize,
    bytes: u64,
    raw_frames: &[(usize, i32)],
    native: bool,
    known_parent: Option<u32>,
) -> Option<(u32, u32)> {
    let mut guard = lock(&STATE);
    let Some(st) = guard.as_mut() else {
        for &(code, _) in raw_frames {
            ffi::Py_DECREF(code as *mut ffi::PyObject);
        }
        return None;
    };
    st.tick();

    let (to_intern, mut stack_id) = match known_parent {
        Some(parent) if !raw_frames.is_empty() => {
            for &(code, _) in &raw_frames[1..] {
                // Interned before, so we already hold a reference, and the
                // frame still owns one: this never reaches zero.
                ffi::Py_DECREF(code as *mut ffi::PyObject);
            }
            (&raw_frames[..1], parent)
        }
        _ => (raw_frames, 0u32),
    };
    let mut parent_id = 0u32;
    for (i, key) in to_intern.iter().enumerate().rev() {
        if i == 0 {
            parent_id = stack_id;
        }
        let frame = match st.frame_ids.get(key) {
            Some(&id) => {
                // Already held. The frame still owns the code object, so this
                // never reaches zero and never runs Python code under the lock.
                ffi::Py_DECREF(key.0 as *mut ffi::PyObject);
                id
            }
            None => {
                if !st.codes.insert(key.0) {
                    ffi::Py_DECREF(key.0 as *mut ffi::PyObject);
                }
                let id = st.frames.len() as u32;
                st.frames.push(*key);
                st.frame_ids.insert(*key, id);
                id
            }
        };
        stack_id = match st.stack_ids.get(&(stack_id, frame)) {
            Some(&id) => id,
            None => {
                let id = st.stack_nodes.len() as u32;
                st.stack_nodes.push((stack_id, frame));
                st.stack_ids.insert((stack_id, frame), id);
                id
            }
        };
    }

    st.push_alloc(addr, bytes, stack_id, native);
    Some((stack_id, parent_id))
}

pub fn record_free(addr: usize, temporary: bool) {
    let mut guard = lock(&STATE);
    let Some(st) = guard.as_mut() else { return };
    if let Some((bytes, stack, native)) = st.live.remove(&addr) {
        st.tick();
        st.ledger.apply(stack, -(bytes as i64), native, st.now_ns);
        if temporary {
            *st.temporaries.entry(stack).or_insert(0) += 1;
        }
    }
}

/// Collect everything into a `Capture`. Caller holds the GIL and has already
/// uninstalled the hooks.
pub unsafe fn finish(sample_rate: u64, native_supported: bool) -> Option<Capture> {
    let mut st = lock(&STATE).take()?;
    let end = st.start.elapsed().as_nanos() as u64;
    st.ledger.timeline.flush(end);

    let mut names: HashMap<usize, (String, String)> = HashMap::new();
    for &code in &st.codes {
        let obj = code as *mut ffi::PyObject;
        names.insert(code, (attr_string(obj, c"co_filename"), attr_string(obj, c"co_qualname")));
    }
    let frames = st
        .frames
        .iter()
        .map(|&(code, lasti)| {
            let (file, func) = names.get(&code).cloned().unwrap_or_else(|| ("?".into(), "?".into()));
            let line = ffi::PyCode_Addr2Line(code as *mut ffi::PyCodeObject, lasti);
            Frame { file, func, line }
        })
        .collect();
    for &code in &st.codes {
        ffi::Py_DECREF(code as *mut ffi::PyObject);
    }

    let peak_by_stack = st.ledger.at_peak();
    let peak_native_bytes = peak_by_stack.values().map(|a| a.native_bytes).sum();
    Some(Capture {
        frames,
        nodes: st.stack_nodes,
        live_at_end: st.ledger.live_now(),
        temporaries: st.temporaries.into_iter().collect(),
        duration_ns: end,
        alloc_count: st.alloc_count,
        alloc_bytes: st.alloc_bytes,
        sample_rate,
        peak_bytes: st.ledger.peak_bytes(),
        peak_t_ns: st.ledger.peak_t_ns(),
        peak_native_bytes,
        peak_by_stack,
        timeline: st.ledger.timeline,
        native_supported,
    })
}
