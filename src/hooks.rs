//! Allocator interception.
//!
//! We wrap CPython's three allocator domains (RAW, MEM, OBJ) with
//! `PyMem_SetAllocator`, the same mechanism tracemalloc uses. Because the OBJ
//! domain is pymalloc, this sees small objects that never reach `malloc`.
//!
//! Rules for everything reachable from these functions:
//! - Never call into Python while holding a lock.
//! - Python C-API calls (frame walking) only happen while
//!   the thread-local `IN_HOOK` guard is set, so allocations they make pass
//!   straight through to the original allocator untracked.
//! - Rust's own allocations go to the system allocator, not PyMem, so the
//!   recorder can use ordinary collections.

use std::cell::Cell;
use std::os::raw::c_void;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use pyo3::ffi;

use crate::recorder;

pub const MAX_DEPTH: usize = 64;

pub static ACTIVE: AtomicBool = AtomicBool::new(false);
/// Mean bytes between samples; 0 records every allocation.
pub static SAMPLE_RATE: AtomicU64 = AtomicU64::new(0);

thread_local! {
    // Const-initialised, no destructors: safe to touch during thread teardown.
    static IN_HOOK: Cell<bool> = const { Cell::new(false) };
    static BYTES_UNTIL_SAMPLE: Cell<i64> = const { Cell::new(i64::MIN) };
    static RNG: Cell<u64> = const { Cell::new(0) };
    static LAST_ALLOC: Cell<usize> = const { Cell::new(0) };
}

#[repr(C)]
struct DomainCtx {
    orig: ffi::PyMemAllocatorEx,
    domain: ffi::PyMemAllocatorDomain,
    is_raw: bool,
}

static mut CTXS: [*mut DomainCtx; 3] = [std::ptr::null_mut(); 3];

const DOMAINS: [ffi::PyMemAllocatorDomain; 3] = [
    ffi::PyMemAllocatorDomain::PYMEM_DOMAIN_RAW,
    ffi::PyMemAllocatorDomain::PYMEM_DOMAIN_MEM,
    ffi::PyMemAllocatorDomain::PYMEM_DOMAIN_OBJ,
];

/// Install the hooks. Caller holds the GIL.
pub unsafe fn install() {
    for (i, &domain) in DOMAINS.iter().enumerate() {
        let mut orig = ffi::PyMemAllocatorEx {
            ctx: std::ptr::null_mut(),
            malloc: None,
            calloc: None,
            realloc: None,
            free: None,
        };
        ffi::PyMem_GetAllocator(domain, &mut orig);
        // Leaked on purpose: another thread may still be inside a hook using
        // this context after we uninstall.
        let ctx = Box::into_raw(Box::new(DomainCtx { orig, domain, is_raw: i == 0 }));
        CTXS[i] = ctx;
        let mut ours = ffi::PyMemAllocatorEx {
            ctx: ctx as *mut c_void,
            malloc: Some(hw_malloc),
            calloc: Some(hw_calloc),
            realloc: Some(hw_realloc),
            free: Some(hw_free),
        };
        ffi::PyMem_SetAllocator(domain, &mut ours);
    }
}

/// Restore the original allocators. Caller holds the GIL.
pub unsafe fn uninstall() {
    for i in 0..3 {
        let ctx = CTXS[i];
        if !ctx.is_null() {
            ffi::PyMem_SetAllocator((*ctx).domain, &mut (*ctx).orig);
            CTXS[i] = std::ptr::null_mut();
        }
    }
}

extern "C" fn hw_malloc(ctx: *mut c_void, size: usize) -> *mut c_void {
    unsafe {
        let d = &*(ctx as *const DomainCtx);
        let p = (d.orig.malloc.unwrap_unchecked())(d.orig.ctx, size);
        if !p.is_null() {
            on_alloc(d.is_raw, p as usize, size);
        }
        p
    }
}

extern "C" fn hw_calloc(ctx: *mut c_void, nelem: usize, elsize: usize) -> *mut c_void {
    unsafe {
        let d = &*(ctx as *const DomainCtx);
        let p = (d.orig.calloc.unwrap_unchecked())(d.orig.ctx, nelem, elsize);
        if !p.is_null() {
            on_alloc(d.is_raw, p as usize, nelem.saturating_mul(elsize));
        }
        p
    }
}

extern "C" fn hw_realloc(ctx: *mut c_void, ptr: *mut c_void, size: usize) -> *mut c_void {
    unsafe {
        let d = &*(ctx as *const DomainCtx);
        // Forget the old block first: once realloc returns, another thread may
        // already own that address.
        if !ptr.is_null() {
            on_free(ptr as usize);
        }
        let p = (d.orig.realloc.unwrap_unchecked())(d.orig.ctx, ptr, size);
        if !p.is_null() {
            on_alloc(d.is_raw, p as usize, size);
        }
        p
    }
}

extern "C" fn hw_free(ctx: *mut c_void, ptr: *mut c_void) {
    unsafe {
        let d = &*(ctx as *const DomainCtx);
        if !ptr.is_null() {
            on_free(ptr as usize);
        }
        (d.orig.free.unwrap_unchecked())(d.orig.ctx, ptr);
    }
}

#[inline]
fn next_random() -> u64 {
    RNG.with(|r| {
        let mut x = r.get();
        if x == 0 {
            // Seed per thread from the address of a stack local and a counter.
            static SEED: AtomicU64 = AtomicU64::new(0x9E37_79B9_7F4A_7C15);
            let local = 0u8;
            x = SEED.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::Relaxed)
                ^ (&local as *const u8 as u64)
                | 1;
        }
        // xorshift64*
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        r.set(x);
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    })
}

/// Exponentially distributed gap with the given mean, as Go and tcmalloc do.
fn next_sample_gap(rate: u64) -> i64 {
    let u = ((next_random() >> 11) as f64 + 0.5) / (1u64 << 53) as f64;
    (-(u.ln()) * rate as f64) as i64 + 1
}

/// Returns the number of bytes this allocation stands for, or `None` if it is
/// not sampled.
#[inline]
fn sample(size: usize) -> Option<u64> {
    let rate = SAMPLE_RATE.load(Ordering::Relaxed);
    if rate == 0 {
        return Some(size as u64);
    }
    BYTES_UNTIL_SAMPLE.with(|c| {
        let mut left = c.get();
        if left == i64::MIN {
            left = next_sample_gap(rate);
        }
        left -= size as i64;
        if left > 0 {
            c.set(left);
            return None;
        }
        c.set(next_sample_gap(rate));
        // Unbiased estimate of the bytes this sample represents.
        let s = size as f64;
        let r = rate as f64;
        let p = 1.0 - (-s / r).exp();
        Some(if p > 0.0 { (s / p).round() as u64 } else { size as u64 })
    })
}

unsafe fn on_alloc(is_raw: bool, addr: usize, size: usize) {
    if !ACTIVE.load(Ordering::Relaxed) {
        return;
    }
    if IN_HOOK.with(|g| g.replace(true)) {
        return;
    }
    if let Some(bytes) = sample(size) {
        let mut frames: [(usize, i32); MAX_DEPTH] = [(0, 0); MAX_DEPTH];
        let depth = if is_raw && ffi::PyGILState_Check() == 0
        {
            0
        } else {
            walk_python_stack(&mut frames)
        };
        recorder::record_alloc(addr, bytes, &frames[..depth]);
        if SAMPLE_RATE.load(Ordering::Relaxed) == 0 {
            LAST_ALLOC.with(|l| l.set(addr));
        }
    } else {
        LAST_ALLOC.with(|l| l.set(0));
    }
    IN_HOOK.with(|g| g.set(false));
}

fn on_free(addr: usize) {
    if !ACTIVE.load(Ordering::Relaxed) {
        return;
    }
    // No Python calls here, so frees are recorded even inside a hook.
    // heaptrack's definition: freed with no other allocation on this thread
    // in between. Other frees do not break the chain.
    let temporary = LAST_ALLOC.with(|l| {
        let t = l.get() == addr;
        if t {
            l.set(0);
        }
        t
    });
    recorder::record_free(addr, temporary);
}

extern "C" {
    // Public since CPython 3.11; not yet declared by pyo3-ffi.
    fn PyFrame_GetLasti(frame: *mut ffi::PyFrameObject) -> std::os::raw::c_int;
}

/// Fill `out` with (code object, bytecode offset) pairs, innermost first.
/// Each code object comes with a new reference that the recorder takes over.
/// Line numbers are resolved once at analysis time, not per allocation.
unsafe fn walk_python_stack(out: &mut [(usize, i32); MAX_DEPTH]) -> usize {
    let mut depth = 0;
    let mut frame = ffi::PyEval_GetFrame();
    if !frame.is_null() {
        ffi::Py_INCREF(frame as *mut ffi::PyObject);
    }
    while !frame.is_null() && depth < MAX_DEPTH {
        let code = ffi::PyFrame_GetCode(frame);
        out[depth] = (code as usize, PyFrame_GetLasti(frame));
        depth += 1;
        let back = ffi::PyFrame_GetBack(frame);
        ffi::Py_DECREF(frame as *mut ffi::PyObject);
        frame = back;
    }
    if !frame.is_null() {
        ffi::Py_DECREF(frame as *mut ffi::PyObject);
    }
    depth
}
