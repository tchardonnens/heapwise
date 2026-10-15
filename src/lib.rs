//! heapwise native core: allocator hooks, recording and analysis.

use std::sync::atomic::Ordering;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

mod analysis;
mod hooks;
mod native;
mod recorder;
mod report;

pub use analysis::Capture;

/// Start recording heap allocations: CPython's allocators everywhere, plus
/// malloc/mmap from native code where the platform is supported.
///
/// `sample_rate` is the mean number of bytes between samples; 0 records every
/// allocation.
#[pyfunction]
#[pyo3(signature = (sample_rate = 0))]
fn start(sample_rate: u64) -> PyResult<()> {
    if hooks::ACTIVE.load(Ordering::SeqCst) {
        return Err(PyRuntimeError::new_err("heapwise is already recording"));
    }
    recorder::begin();
    hooks::SAMPLE_RATE.store(sample_rate, Ordering::SeqCst);
    unsafe { hooks::install() };
    hooks::NATIVE.store(native::install(), Ordering::SeqCst);
    hooks::ACTIVE.store(true, Ordering::SeqCst);
    Ok(())
}

/// Stop recording and return the analysed capture.
#[pyfunction]
fn stop() -> PyResult<Capture> {
    if !hooks::ACTIVE.swap(false, Ordering::SeqCst) {
        return Err(PyRuntimeError::new_err("heapwise is not recording"));
    }
    native::uninstall();
    unsafe { hooks::uninstall() };
    let rate = hooks::SAMPLE_RATE.load(Ordering::SeqCst);
    let native = hooks::NATIVE.load(Ordering::SeqCst);
    unsafe { recorder::finish(rate, native) }.ok_or_else(|| PyRuntimeError::new_err("no capture in progress"))
}

#[pyfunction]
fn is_recording() -> bool {
    hooks::ACTIVE.load(Ordering::SeqCst)
}

#[pyfunction]
fn format_bytes(n: u64) -> String {
    report::human_bytes(n)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(start, m)?)?;
    m.add_function(wrap_pyfunction!(stop, m)?)?;
    m.add_function(wrap_pyfunction!(is_recording, m)?)?;
    m.add_function(wrap_pyfunction!(format_bytes, m)?)?;
    m.add_class::<Capture>()?;
    Ok(())
}
