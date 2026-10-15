//! Cheap event recording on the allocation path.
//!
//! Following heaptrack: record each event with as little work as possible and
//! leave all aggregation (peak, leaks, temporaries) and symbolization (names,
//! line numbers) to analysis time.

use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, MutexGuard};
use std::time::Instant;

use pyo3::ffi;

use crate::analysis::{Agg, Capture, Event, Frame};

struct State {
    start: Instant,
    /// Code objects we hold a strong reference to, so each pointer stays
    /// unique for the whole capture.
    codes: HashSet<usize>,
    frame_ids: HashMap<(usize, i32), u32>,
    frames: Vec<(usize, i32)>,
    stack_ids: HashMap<Vec<u32>, u32>,
    stacks: Vec<Vec<u32>>,
    live: HashMap<usize, (u64, u32, bool)>,
    events: Vec<Event>,
    temporaries: HashMap<u32, u64>,
    alloc_count: u64,
    alloc_bytes: u64,
}

static STATE: Mutex<Option<State>> = Mutex::new(None);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn begin() {
    *lock(&STATE) = Some(State {
        start: Instant::now(),
        codes: HashSet::new(),
        frame_ids: HashMap::new(),
        frames: Vec::new(),
        stack_ids: HashMap::new(),
        stacks: Vec::new(),
        live: HashMap::new(),
        events: Vec::with_capacity(1 << 16),
        temporaries: HashMap::new(),
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

/// `raw_frames` holds (code object, bytecode offset) pairs, each code object
/// with a new reference that this function takes over.
pub unsafe fn record_alloc(addr: usize, bytes: u64, raw_frames: &[(usize, i32)], native: bool) {
    let mut guard = lock(&STATE);
    let Some(st) = guard.as_mut() else {
        for &(code, _) in raw_frames {
            ffi::Py_DECREF(code as *mut ffi::PyObject);
        }
        return;
    };
    let t_ns = st.start.elapsed().as_nanos() as u64;

    let mut stack = Vec::with_capacity(raw_frames.len());
    for key in raw_frames {
        if !st.codes.insert(key.0) {
            // Already held. The frame still owns the code object, so this
            // never reaches zero and never runs Python code under the lock.
            ffi::Py_DECREF(key.0 as *mut ffi::PyObject);
        }
        let id = match st.frame_ids.get(key) {
            Some(&id) => id,
            None => {
                let id = st.frames.len() as u32;
                st.frames.push(*key);
                st.frame_ids.insert(*key, id);
                id
            }
        };
        stack.push(id);
    }
    let stack_id = match st.stack_ids.get(&stack) {
        Some(&id) => id,
        None => {
            let id = st.stacks.len() as u32;
            st.stacks.push(stack.clone());
            st.stack_ids.insert(stack, id);
            id
        }
    };

    if let Some((old_bytes, old_stack, old_native)) = st.live.insert(addr, (bytes, stack_id, native)) {
        // A free we never saw (e.g. memory released inside libc).
        st.events.push(Event { t_ns, delta: -(old_bytes as i64), stack: old_stack, native: old_native });
    }
    st.events.push(Event { t_ns, delta: bytes as i64, stack: stack_id, native });
    st.alloc_count += 1;
    st.alloc_bytes += bytes;
}

pub fn record_free(addr: usize, temporary: bool) {
    let mut guard = lock(&STATE);
    let Some(st) = guard.as_mut() else { return };
    if let Some((bytes, stack, native)) = st.live.remove(&addr) {
        let t_ns = st.start.elapsed().as_nanos() as u64;
        st.events.push(Event { t_ns, delta: -(bytes as i64), stack, native });
        if temporary {
            *st.temporaries.entry(stack).or_insert(0) += 1;
        }
    }
}

/// Collect everything into a `Capture`. Caller holds the GIL and has already
/// uninstalled the hooks.
pub unsafe fn finish(sample_rate: u64, native_supported: bool) -> Option<Capture> {
    let st = lock(&STATE).take()?;

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

    let mut live_by_stack: HashMap<u32, Agg> = HashMap::new();
    for &(bytes, stack, native) in st.live.values() {
        let e = live_by_stack.entry(stack).or_default();
        e.bytes += bytes;
        e.native_bytes += if native { bytes } else { 0 };
        e.count += 1;
    }

    Some(Capture::new(
        frames,
        st.stacks,
        st.events,
        live_by_stack,
        st.temporaries,
        st.start.elapsed().as_nanos() as u64,
        st.alloc_count,
        st.alloc_bytes,
        sample_rate,
        native_supported,
    ))
}
