//! snail_rs — PyO3 bindings over `snail-core`.
//!
//! Phase 1 exposes the endpointing VAD (the TTFB-critical piece) and the full audio [`Pipeline`]
//! so the existing Python bridge can call the Rust hot path directly. Later phases migrate the
//! orchestration layer up here too.

use pyo3::prelude::*;

mod pipeline;
mod vad;

/// Crate version (mirrors the Cargo package version) — a cheap import smoke-test from Python.
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn snail_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_class::<vad::EnergyVad>()?;
    m.add_class::<pipeline::Pipeline>()?;
    Ok(())
}
